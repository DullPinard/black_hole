# -*- coding: utf-8 -*-
"""
Black-hole widget with a top-down (face-on) animated accretion disk.
GPU path only — requires PyTorch with CUDA.

Controls:
    drag:    left-click inside the event horizon
    scroll:  resize the event horizon
    A:       toggle drifting motion + edge repulsion
    space:   toggle accretion disk
    R:       toggle "recordable" (disable WDA_EXCLUDEFROMCAPTURE)
    Esc:     quit
"""

import os
import sys
import time
import math
import ctypes
import threading

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import numpy as np
import mss
import win32api
import win32con
import win32gui

import torch
if not torch.cuda.is_available():
    raise RuntimeError("CUDA-capable PyTorch required. See README.md.")


# ---------- tunable parameters --------------------------------------------- #
EINSTEIN_RADIUS  = 90.0
RENDER_SCALE     = 200
COLORKEY         = (255, 0, 255)
TARGET_FPS       = 60
BG_INTERVAL      = 0.01

R_E_MAX_FACTOR   = 0.82

# Accretion disk
DISK_INNER_FAC   = 1.05
DISK_OUTER_FAC   = 3.4
DISK_ROT_SPEED   = 1.2
DISK_TURB_OCT    = 4
DISK_WARP_AMP    = 0.55
DISK_HOT_GAIN    = 1.4
DISK_HOT_RFRAC   = 0.45
DISK_INNER_GLOW  = 1.8
DISK_PEAK        = 3.0
DISK_MAX_ALPHA   = 0.45

# Drift mode
DRIFT_SPEED      = 240.0
EDGE_FORCE_K     = 9.0e6
EDGE_FORCE_EPS   = 25.0
OUT_PULL         = 1500.0
OUT_DAMPING      = 0.25

WDA_EXCLUDEFROMCAPTURE = 0x11
SM_CXSCREEN = 0
SM_CYSCREEN = 1


def get_screen_size():
    u = ctypes.windll.user32
    return u.GetSystemMetrics(SM_CXSCREEN), u.GetSystemMetrics(SM_CYSCREEN)


class Background:
    def __init__(self):
        self.data_gpu = None
        self.w = 0
        self.h = 0
        self.version = 0
        self._stop = threading.Event()
        self._frozen = threading.Event()

    def _push(self, rgb):
        t = torch.from_numpy(rgb).to('cuda', non_blocking=True)
        self.data_gpu = t.to(torch.float32)
        self.version += 1

    def initial_grab(self):
        with mss.mss() as sct:
            mon = sct.monitors[1]
            self.w, self.h = mon["width"], mon["height"]
            raw = np.array(sct.grab(mon))
            self._push(np.ascontiguousarray(raw[:, :, 2::-1]))

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def freeze(self):  self._frozen.set()
    def thaw(self):    self._frozen.clear()

    def _loop(self):
        with mss.mss() as sct:
            mon = sct.monitors[1]
            while not self._stop.is_set():
                if not self._frozen.is_set():
                    raw = np.array(sct.grab(mon))
                    self._push(np.ascontiguousarray(raw[:, :, 2::-1]))
                time.sleep(BG_INTERVAL)

    def stop(self):
        self._stop.set()


def make_layered(hwnd, click_through: bool):
    ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    ex |= (win32con.WS_EX_LAYERED | win32con.WS_EX_TOPMOST
           | win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE)
    if click_through:
        ex |= win32con.WS_EX_TRANSPARENT
    else:
        ex &= ~win32con.WS_EX_TRANSPARENT
    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)
    win32gui.SetLayeredWindowAttributes(
        hwnd, win32api.RGB(*COLORKEY), 0, win32con.LWA_COLORKEY)
    win32gui.SetWindowPos(
        hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
    ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)


def set_click_through(hwnd, enabled: bool):
    ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    if enabled:
        ex |= win32con.WS_EX_TRANSPARENT
    else:
        ex &= ~win32con.WS_EX_TRANSPARENT
    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)


def set_recordable(hwnd, recordable: bool):
    flag = 0 if recordable else WDA_EXCLUDEFROMCAPTURE
    ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, flag)


# __PART2__
def precompute_lens(W, H, cx, cy, r_e, r_out):
    dev = 'cuda'
    yy, xx = torch.meshgrid(
        torch.arange(H, device=dev, dtype=torch.float32),
        torch.arange(W, device=dev, dtype=torch.float32),
        indexing='ij')
    dy = yy - cy
    dx = xx - cx
    r = torch.sqrt(dx * dx + dy * dy)
    r_safe = torch.clamp(r, min=1e-3)
    t = torch.clamp((r - r_e) / max(r_out - r_e, 1.0), 0.0, 1.0)
    win = (1.0 - t) ** 2
    deflect = (r_e * r_e / r_safe) * win
    r_src = r - deflect
    shadow  = r_src <= 0.0
    outside = r > r_out
    factor = torch.where(shadow, torch.zeros_like(r), r_src / r_safe)
    src_dx = dx * factor
    src_dy = dy * factor
    return src_dx, src_dy, shadow, outside, dx, dy, r, win


def _fbm(x, y, octaves):
    s = torch.zeros_like(x)
    amp = 0.5
    fx, fy = 1.0, 1.0
    for k in range(octaves):
        s = s + amp * torch.cos(fx * x + 1.7 * k) * torch.sin(fy * y - 0.9 * k)
        amp *= 0.55
        fx *= 1.93
        fy *= 1.91
    return 0.5 + 0.5 * torch.tanh(s)


def disk_field(dx, dy, r, win, shadow, r_e, t):
    d_in  = DISK_INNER_FAC * r_e
    d_out = DISK_OUTER_FAC * r_e
    rho = torch.clamp(r, min=1e-3)
    in_ring = (rho >= d_in) & (rho <= d_out) & (~shadow)

    omega = DISK_ROT_SPEED * (d_in / torch.clamp(rho, min=d_in)) ** 1.5
    theta = torch.atan2(dy, dx) - omega * t

    A = (rho / r_e) * 2.5
    u = torch.cos(theta) * A
    v_a = torch.sin(theta) * A
    v_r = (rho - d_in) / r_e * 4.0

    warp = _fbm(u * 0.4 + 0.3 * t, v_a * 0.4 - 0.2 * t, DISK_TURB_OCT - 1)
    u2 = u + DISK_WARP_AMP * (warp - 0.5) * 4.0
    v2 = v_a + DISK_WARP_AMP * (warp - 0.5) * 4.0
    smoke = _fbm(u2 + 0.3 * v_r, v2 + 0.6 * t, DISK_TURB_OCT)

    rad_t = torch.clamp((rho - d_in) / max(d_out - d_in, 1.0), 0.0, 1.0)
    radial = (1.0 - rad_t) ** 1.3
    inner_glow = torch.exp(-((rad_t - 0.04) ** 2) / 0.02) * DISK_INNER_GLOW

    hot_rho = d_in + DISK_HOT_RFRAC * (d_out - d_in)
    hot_omega = DISK_ROT_SPEED * (d_in / hot_rho) ** 1.5
    hot_phase = -hot_omega * t
    hx = hot_rho * math.cos(hot_phase)
    hy = hot_rho * math.sin(hot_phase)
    sigma = 0.28 * r_e
    d_hot = (dx - hx) ** 2 + (dy - hy) ** 2
    hot = torch.exp(-d_hot / (2.0 * sigma * sigma)) * DISK_HOT_GAIN

    density = in_ring.to(torch.float32) * (
        radial * (0.35 + 0.9 * smoke) + inner_glow + hot
    )
    density = density * win
    return torch.clamp(density, 0.0, 4.0)


def disk_rgba(density):
    d = torch.clamp(density / DISK_PEAK, 0.0, 1.0)
    R = torch.full_like(d, 255.0)
    G = 130.0 + 110.0 * d
    B = 30.0 + 200.0 * (d ** 2)
    rgb = torch.stack([R, G, B], dim=-1)
    alpha = (d ** 0.65) * DISK_MAX_ALPHA
    return rgb, alpha


# __PART3__
def main():
    W, H = get_screen_size()

    bg = Background()
    bg.initial_grab()

    os.environ.setdefault("SDL_VIDEO_CENTERED", "0")
    os.environ.setdefault("SDL_VIDEO_WINDOW_POS", "0,0")
    import pygame
    pygame.display.init()
    surf = pygame.display.set_mode((W, H), pygame.NOFRAME)
    pygame.display.set_caption("BlackHoleDisk")
    hwnd = pygame.display.get_wm_info()["window"]
    make_layered(hwnd, click_through=False)
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST,
                          0, 0, W, H, win32con.SWP_NOACTIVATE)
    bg.start()

    cx = W // 2
    cy = H // 2
    r_e = float(EINSTEIN_RADIUS)
    r_out = r_e * RENDER_SCALE
    disk_on = True

    state = {}

    def relens():
        nonlocal r_out
        r_out = min(r_e * RENDER_SCALE, float(min(W, H)) * 1.5 - 4.0)
        (state['sdx'], state['sdy'], state['shadow'], state['outside'],
         state['dx'], state['dy'], state['r'], state['win']) = \
            precompute_lens(W, H, cx, cy, r_e, r_out)

    ck = torch.tensor(COLORKEY, dtype=torch.uint8, device='cuda')
    relens()
    surf_buf = np.empty((W, H, 3), dtype=np.uint8)

    def render(t_now):
        b = bg.data_gpu
        if b is None:
            return
        bH, bW, _ = b.shape
        sx = (state['sdx'] + cx).clamp_(0, bW - 1).to(torch.long)
        sy = (state['sdy'] + cy).clamp_(0, bH - 1).to(torch.long)
        sample = b[sy, sx]
        if disk_on:
            density = disk_field(state['dx'], state['dy'],
                                 state['r'], state['win'],
                                 state['shadow'], r_e, t_now)
            rgb, alpha = disk_rgba(density)
            a = alpha.unsqueeze(-1)
            sample = sample * (1.0 - a) + rgb * a
        sample = torch.where(state['shadow'].unsqueeze(-1),
                             torch.zeros_like(sample), sample)
        sample = torch.clamp(sample, 0.0, 255.0)
        out = sample.to(torch.uint8)
        out[state['outside']] = ck
        np.copyto(surf_buf, out.permute(1, 0, 2).contiguous().cpu().numpy())
        pygame.surfarray.blit_array(surf, surf_buf)
        pygame.display.flip()

    # ---------- main loop ------------------------------------------------- #
    clock = pygame.time.Clock()
    dragging = False
    drag_anchor = (0, 0)
    GetAsync = ctypes.windll.user32.GetAsyncKeyState
    click_through = False
    R_E_MIN = 20.0
    R_E_MAX = float(min(W, H)) * R_E_MAX_FACTOR
    WHEEL_STEP = 1.10

    drift = False
    vx = vy = 0.0
    fcx, fcy = float(cx), float(cy)
    t0 = time.time()
    prev_a = prev_space = prev_r = False
    recordable = False

    running = True
    while running:
        dt = max(clock.tick(TARGET_FPS) / 1000.0, 1e-3)
        if GetAsync(win32con.VK_ESCAPE) & 0x8000:
            break

        a_now     = bool(GetAsync(ord('A')) & 0x8000)
        space_now = bool(GetAsync(win32con.VK_SPACE) & 0x8000)
        r_now     = bool(GetAsync(ord('R')) & 0x8000)
        if r_now and not prev_r:
            recordable = not recordable
            set_recordable(hwnd, recordable)
            # When the widget becomes visible to capture APIs, the
            # background grabber would otherwise re-record the BH itself
            # — feeding warped output back into the lens and quickly
            # collapsing the image to black. Lock the desktop snapshot
            # while in record mode and resume live capture on toggle off.
            if recordable:
                bg.freeze()
            else:
                bg.thaw()
        if a_now and not prev_a:
            drift = not drift
            if drift:
                ang = np.random.uniform(0.0, 2.0 * np.pi)
                vx = DRIFT_SPEED * np.cos(ang)
                vy = DRIFT_SPEED * np.sin(ang)
                fcx, fcy = float(cx), float(cy)
            else:
                vx = vy = 0.0
        if space_now and not prev_space:
            disk_on = not disk_on
        prev_a, prev_space, prev_r = a_now, space_now, r_now

        mx, my = win32api.GetCursorPos()
        in_horizon = (mx - cx) ** 2 + (my - cy) ** 2 <= r_e * r_e
        want_through = (not in_horizon) and (not dragging)
        if want_through != click_through:
            set_click_through(hwnd, want_through)
            click_through = want_through

        lens_dirty = False
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                if in_horizon:
                    drag_anchor = (mx - cx, my - cy)
                    dragging = True
                    drift = False
                    vx = vy = 0.0
            elif ev.type == pygame.MOUSEWHEEL:
                if ev.y > 0:
                    r_e = min(R_E_MAX, r_e * WHEEL_STEP)
                elif ev.y < 0:
                    r_e = max(R_E_MIN, r_e / WHEEL_STEP)
                lens_dirty = True

        if dragging:
            if not (GetAsync(win32con.VK_LBUTTON) & 0x8000):
                dragging = False
            else:
                new_cx = mx - drag_anchor[0]
                new_cy = my - drag_anchor[1]
                if (new_cx, new_cy) != (cx, cy):
                    cx, cy = new_cx, new_cy
                    fcx, fcy = float(cx), float(cy)
                    lens_dirty = True
        elif drift:
            cx_t = W * 0.5
            cy_t = H * 0.5

            def accel(x, y):
                if 0.0 <= x <= W and 0.0 <= y <= H:
                    dl = max(x,     EDGE_FORCE_EPS)
                    dr = max(W - x, EDGE_FORCE_EPS)
                    du = max(y,     EDGE_FORCE_EPS)
                    dd = max(H - y, EDGE_FORCE_EPS)
                    ax = EDGE_FORCE_K * (1.0 / (dl * dl) - 1.0 / (dr * dr))
                    ay = EDGE_FORCE_K * (1.0 / (du * du) - 1.0 / (dd * dd))
                    return ax, ay
                ddx = cx_t - x
                ddy = cy_t - y
                n = (ddx * ddx + ddy * ddy) ** 0.5
                if n < 1e-3:
                    return 0.0, 0.0
                return OUT_PULL * ddx / n, OUT_PULL * ddy / n

            ax0, ay0 = accel(fcx, fcy)
            fcx += vx * dt + 0.5 * ax0 * dt * dt
            fcy += vy * dt + 0.5 * ay0 * dt * dt
            ax1, ay1 = accel(fcx, fcy)
            vx += 0.5 * (ax0 + ax1) * dt
            vy += 0.5 * (ay0 + ay1) * dt

            if not (0.0 <= fcx <= W and 0.0 <= fcy <= H):
                k = OUT_DAMPING ** dt
                vx *= k
                vy *= k

            new_cx = int(round(fcx))
            new_cy = int(round(fcy))
            if (new_cx, new_cy) != (cx, cy):
                cx, cy = new_cx, new_cy
                lens_dirty = True

        if lens_dirty:
            relens()

        render(time.time() - t0)

    bg.stop()
    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()

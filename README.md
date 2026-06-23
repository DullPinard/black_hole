# Desktop Black Hole

一个 Windows 桌面悬浮黑洞插件。整张屏幕是它的画布：透镜把光线扭进
事件视界，外圈生成一个动态吸积盘，按 `A` 让它在四壁之间真实弹跳，
所有合成在 GPU 上完成，跟桌面无缝叠合。

![demo](demo.png)

## 控制

| 按键 / 操作 | 行为 |
| --- | --- |
| 左键拖拽（在事件视界内） | 移动黑洞 |
| 滚轮 | 放大 / 缩小事件视界 |
| `A` | 切换漂移模式（势能 + 动能 + 边缘弹跳） |
| `Space` | 切换吸积盘 |
| `R` | 切换"可被录屏"（默认对截图通道隐身） |
| `Esc` | 退出 |

## 运行环境

- Windows 10 / 11（用了 `WS_EX_LAYERED`、`SetWindowDisplayAffinity`、
  `SetProcessDpiAwareness` 等 Win32 API）
- NVIDIA GPU + CUDA 驱动
- Python 3.9+ 与 PyTorch CUDA 版本

```bash
# 推荐用 conda
conda create -n blackhole python=3.10 -y
conda activate blackhole

# CUDA 版 torch 从官方 index 装（CUDA 12.4 对应 cu124）
pip install torch --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements.txt

python main.py
```

## 物理原理

### 从爱因斯坦场方程到光线偏折

广义相对论的核心是爱因斯坦场方程

$$
R_{\mu\nu} - \tfrac{1}{2} g_{\mu\nu} R + \Lambda g_{\mu\nu}
= \frac{8\pi G}{c^4} T_{\mu\nu}
$$

把它在球对称真空（$T_{\mu\nu} = 0,\ \Lambda = 0$）下求解，得到的就是
Schwarzschild 度规

$$
\mathrm{d}s^2 = -\left(1 - \frac{r_s}{r}\right) c^2 \mathrm{d}t^2
              + \left(1 - \frac{r_s}{r}\right)^{-1} \mathrm{d}r^2
              + r^2 \left(\mathrm{d}\theta^2 + \sin^2\theta\, \mathrm{d}\varphi^2\right)
$$

其中 $r_s = 2GM / c^2$ 是史瓦西半径。让光子按零测地线 $\mathrm{d}s = 0$
运动，并令冲击参数 $b$（即光线在无穷远处距黑洞中心的最近距离），积分

$$
\varphi(b) = 2 \int_{r_0}^{\infty}
   \frac{\mathrm{d}r}{r^2 \sqrt{\frac{1}{b^2} - \frac{1}{r^2}\!\left(1 - \frac{r_s}{r}\right)}}
   - \pi
$$

在弱场极限 $b \gg r_s$ 下展开到首阶，得到经典的偏折角

$$
\alpha(b) = \frac{4 G M}{c^2 b} = \frac{2 r_s}{b}
$$

### 薄透镜方程

把黑洞当作几何上无厚度的"透镜面"，源平面、透镜面、观察者三层之间用
角度变量 $\beta$（源真实角位置）、$\theta$（观察者看到的角位置）联立

$$
\beta = \theta - \frac{D_{LS}}{D_S} \alpha(D_L \theta)
$$

代入 $\alpha = 2 r_s / b$ 并令爱因斯坦角半径

$$
\theta_E = \sqrt{\frac{2 r_s D_{LS}}{D_L D_S}}
$$

就得到这个项目实际使用的"透镜方程"

$$
\boxed{\;\beta = \theta - \frac{\theta_E^2}{\theta}\;}
$$

它有一对解 $\theta_{\pm}$；本项目只取直接路径分支，所以在屏幕像素
半径 $r$ 上写成

$$
r_{\text{src}}(r) = r - \frac{r_E^2}{r}
$$

`r ≤ r_E` 区域 $r_{\text{src}} \le 0$，对应被光子球俘获的光线，渲染
成纯黑事件视界阴影。其余像素的方向不变，距离收缩：

$$
\bigl(s_x,\, s_y\bigr) = \bigl(d_x,\, d_y\bigr) \cdot \frac{r_{\text{src}}}{r}
$$

GPU 直接按这个偏移做反向纹理采样。

### 边缘衰减窗

纯 $1/r$ 偏折在远处仍有微弱形变，会让圆盘边缘与桌面无法完美对齐。
在偏折项上乘一个二次衰减窗

$$
w(r) = (1 - t)^2,\quad
t = \mathrm{clamp}\!\left(\frac{r - r_E}{r_{\text{out}} - r_E},\; 0,\; 1\right)
$$

$$
\mathrm{deflect}(r) = \frac{r_E^2}{r} \cdot w(r)
$$

$r_{\text{out}}$ 处偏折严格归零，圆盘外沿与桌面像素一一对应。
$r_{\text{out}}$ 跟随 $r_E$ 等比例缩放（`RENDER_SCALE` 倍），所以
缩放过程中视觉边界永远稳定。

### 吸积盘：俯视角动态发光场

俯视吸积盘（face-on）的密度场用了几个简单要素叠加：

1. **开普勒差速旋转**。轨道角速度

   $$
   \omega(\rho) = \omega_0 \left(\frac{r_{\text{in}}}{\rho}\right)^{3/2}
   $$

   内圈转得比外圈快，方位角相位 $\theta' = \theta - \omega t$。

2. **2π 闭合的 FBM 噪声**。把 $(\cos\theta',\,\sin\theta')\cdot A(\rho)$
   当作输入坐标喂给 fractal-Brownian-motion

   $$
   F(\mathbf p) = \sum_{k=0}^{N-1} a^k \cos(f^k p_x + 1.7 k)
                                    \sin(f^k p_y - 0.9 k)
   $$

   转一整圈回到起点，避免 $\theta = \pm\pi$ 处的接缝。

3. **域畸变（domain warp）**。先用一层小尺度 FBM 抖动主噪声的查询
   坐标 $\mathbf p \to \mathbf p + A_w (F(\mathbf p) - 0.5)$，让条带
   不是规则的圆环而是不规则的湍流烟雾。

4. **内沿亮环 + 公转热斑**。$r \approx r_E$ 处叠一个高斯亮带模拟
   内边界等离子体；再加一个绕中心公转的高斯热斑做局部亮度调制。

5. **温度梯度配色 + alpha 合成**。颜色由密度自身驱动（密度→温度→
   黑体色），不透明度

   $$
   \alpha = \left(\frac{d}{D_{\text{peak}}}\right)^{0.65}\!\cdot\, \alpha_{\max}
   $$

   最亮处也保留 ~18% 透明度，所以背景不会被糊死。

最终颜色用经典 Porter–Duff over 合成

$$
\mathbf c = (1 - \alpha)\, \mathbf c_{\text{bg}} + \alpha\, \mathbf c_{\text{disk}}
$$

### 漂移：势能 + 动能的弹跳模型

按 `A` 时给黑洞一个均匀随机方向的初速 $\mathbf v_0$。屏幕内运动在
四面墙合成的反斥势 V 中演化

$$
V(x, y) = K \left(\frac{1}{x} + \frac{1}{W - x}
                 + \frac{1}{y} + \frac{1}{H - y}\right)
$$

$$
\mathbf F = -\nabla V
\;\Longrightarrow\;
F_x = K \left(\frac{1}{x^2} - \frac{1}{(W - x)^2}\right),\quad
F_y = K \left(\frac{1}{y^2} - \frac{1}{(H - y)^2}\right)
$$

用 velocity-Verlet 辛积分推进

$$
\begin{aligned}
\mathbf r_{n+1} &= \mathbf r_n + \mathbf v_n\, \Delta t
                  + \tfrac{1}{2}\, \mathbf a_n\, \Delta t^2 \\
\mathbf v_{n+1} &= \mathbf v_n
                  + \tfrac{1}{2}\, \bigl(\mathbf a_n + \mathbf a_{n+1}\bigr)\, \Delta t
\end{aligned}
$$

这套积分器守恒能量 $E = \tfrac{1}{2} m |\mathbf v|^2 + V(\mathbf r)$，
所以黑洞会在四壁之间不断弹跳，路径就像台球——这是"反射"的来源。

如果意外飞出屏幕，加速度切换为指向中心的恒定矢量 $\mathbf a = a_0\,\hat{\mathbf c}$
（$\hat{\mathbf c}$ 是单位回中心向量），同时对速度乘以指数阻尼

$$
\mathbf v \leftarrow \mathbf v \cdot \gamma^{\Delta t},\quad \gamma < 1
$$

能量被人为拿走，黑洞被拽回屏幕内并在内圈势阱里收敛。

## 实现要点

### 全屏覆盖、点击穿透、桌面采样

- `SetProcessDpiAwareness(PER_MONITOR_AWARE)` 必须在 `GetSystemMetrics`
  和创建窗口前调用，否则 Windows 会按系统缩放返回逻辑像素，SDL
  窗口只会盖住物理屏幕的一部分，这就是各种"看到边界"的根源。
- 窗口属性：`WS_EX_LAYERED + WS_EX_TOPMOST + WS_EX_TOOLWINDOW +
  WS_EX_NOACTIVATE`，配合 `LWA_COLORKEY` 把外圈洋红色像素抠成
  系统级透明。
- 鼠标穿透不靠 colorkey，靠 `WS_EX_TRANSPARENT` 的运行时切换：
  指针在事件视界内时关掉 transparent flag 接收拖拽，离开后立刻打开
  让点击穿到下层应用。
- 背景由 daemon 线程用 `mss` 持续抓取主屏，每帧上传到 GPU 张量
  作为透镜的"远端图"。

### 焦点无关的全局键盘

由于 `WS_EX_NOACTIVATE` 永不获焦，`pygame.KEYDOWN` 收不到事件。
所有快捷键（`A` / `Space` / `R` / `Esc`）都用 `GetAsyncKeyState`
轮询，并做"上一帧按下"边沿检测，避免按住连翻。

### GPU 渲染管线

每帧只在 GPU 上做：

1. 重算 lens 网格（仅当中心或半径变化）：`(src_dx, src_dy)` 离散
   场 + `shadow` / `outside` 布尔掩码。
2. 高级花式索引 `bg[sy, sx]` 反向采样桌面纹理。
3. 计算吸积盘 density → 颜色 / alpha → over 合成。
4. 视界 shadow 涂黑、外圈写 colorkey。
5. `permute(1,0,2).cpu().numpy()` 拷回主存喂给 SDL surface。

CPU 主线程只做窗口管理和事件循环，1080p / 1440p 全屏 60 FPS 满帧
没压力（实测 RTX 4060 Laptop）。

### 录屏可见性

默认调 `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` 把窗口
从所有 OS 截图通道隐藏，避免后台抓屏线程把自己拍进去造成无限
反馈。按 `R` 摘掉这个 flag 之后 OBS / NVIDIA Shadowplay 等录屏
工具就能录到了。

**注意**：进入录屏模式时背景会被冻结到当前快照。原因很简单——一旦
窗口对截图通道可见，`mss` 抓到的画面里就含黑洞自己，再喂给透镜会
把变形结果再扭一次，几帧之内就会塌成一团黑。所以 `R` 切到录屏
模式时主程序会调用 `bg.freeze()` 锁住上一次背景；切回隐藏时
`bg.thaw()` 恢复实时采样。

## 可调参数

所有参数集中在 [main.py](main.py) 顶部：

| 参数 | 说明 |
| --- | --- |
| `EINSTEIN_RADIUS` | 启动时事件视界半径（像素） |
| `RENDER_SCALE` | 可视圆盘外径 / 视界 比值 |
| `R_E_MAX_FACTOR` | 视界最大半径占屏幕短边的比例 |
| `DISK_INNER_FAC / DISK_OUTER_FAC` | 吸积盘内 / 外径相对 r_E 倍数 |
| `DISK_ROT_SPEED` | 外缘角速度（rad/s） |
| `DISK_TURB_OCT / DISK_WARP_AMP` | 湍流强度与畸变幅度 |
| `DISK_PEAK / DISK_MAX_ALPHA` | 亮度归一化基准与最高不透明度 |
| `DRIFT_SPEED` | A 键启动时的初速 |
| `EDGE_FORCE_K` | 边缘反斥力强度 |
| `OUT_PULL / OUT_DAMPING` | 越界后的回拉力与阻尼 |

## 参考

- Schwarzschild, K. (1916), Über das Gravitationsfeld eines Massenpunktes
- Bartelmann, M. (2010), Gravitational Lensing, Class. Quantum Grav. 27
- Luminet, J.-P. (1979), Image of a spherical black hole with thin
  accretion disk

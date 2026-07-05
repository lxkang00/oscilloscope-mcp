# Oscilloscope MCP Server

基于 **SDS2000X Plus Programming Guide CN11G** 编写的 MCP (Model Context Protocol) Server，通过 TCP/IP (SCPI 协议，端口 5024) 远程控制 Siglent SDS2000X Plus 系列示波器。

## 硬件要求

- Siglent SDS2000X Plus 系列示波器（也兼容 SDS1000X, SDS2000X, SDS5000X 等系列）
- 示波器通过 LAN 连接，已知 IP 地址

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 MCP（在 Claude Code 的 .mcp.json 中添加）
# 路径指向本目录下的 server.py

# 3. 在 Claude Code 中使用
```

## 配置 Claude Code

**方式一：项目级配置**（推荐）

将本目录下的 `.mcp.json` 复制到你的项目根目录，或合并到已有的 `.mcp.json` 中：

```json
{
  "mcpServers": {
    "oscilloscope": {
      "command": "python",
      "args": ["path/to/oscilloscope-mcp/server.py"]
    }
  }
}
```

**方式二：全局配置**

在 `~/.claude/.mcp.json`（Linux/macOS）或 `%USERPROFILE%\.claude\.mcp.json`（Windows）中添加上述配置。

## 通信协议

| 项目 | 说明 |
|------|------|
| 物理层 | Ethernet (TCP/IP) |
| 端口 | **5024** |
| 协议 | Raw TCP Socket (Telnet 兼容) |
| 命令 | IEEE 488.2 + SCPI |
| 编码 | ASCII，每行以 `\n` 结尾 |

## 可用工具（29 个）

### 连接管理
| 工具 | 说明 |
|------|------|
| `connect` | 通过 IP 连接示波器（必须先调用） |
| `disconnect` | 断开连接 |
| `send_command` | 发送任意 SCPI 命令（万能逃生舱） |
| `get_id` | 获取设备 ID (*IDN?) |
| `reset` | 恢复出厂设置 (*RST) |

### 通道配置
| 工具 | 说明 |
|------|------|
| `configure_channel` | 配置通道（显示/刻度/耦合/探头/带宽/反转/标签/单位/偏移校准） |
| `get_channel` | 读取通道当前设置 |

### 时基
| 工具 | 说明 |
|------|------|
| `configure_timebase` | 设置水平时基（扫描速度/延迟/模式：MAIN/WINDOW/ROLL/XY） |
| `get_timebase` | 读取时基设置 |

### 触发
| 工具 | 说明 |
|------|------|
| `configure_trigger` | 配置触发（模式/类型/源/电平/斜率/耦合/释抑） |
| `get_trigger` | 读取触发设置 |
| `force_trigger` | 强制触发 |

### 采集
| 工具 | 说明 |
|------|------|
| `configure_acquisition` | 配置采集（模式/存储深度/插值/平均次数） |
| `get_acquisition` | 读取采集设置（含当前采样率） |

### 测量
| 工具 | 说明 |
|------|------|
| `measure` | 执行自动测量（VPP/FREQ/RMS/RISE/FALL/...共 50+ 种） |
| `get_measure_stats` | 获取测量统计（均值/最小值/最大值/标准差/计数） |
| `clear_measure_stats` | 清除测量统计 |

### 波形数据
| 工具 | 说明 |
|------|------|
| `get_waveform_preamble` | 获取波形元数据（格式/点数/X增量/Y增量/原点） |
| `get_waveform_data` | 获取波形电压数据（支持降采样） |

### 显示
| 工具 | 说明 |
|------|------|
| `configure_display` | 配置显示（网格/余辉/亮度/网格样式/坐标轴） |

### 光标
| 工具 | 说明 |
|------|------|
| `configure_cursors` | 配置光标测量（模式/源/类型） |
| `get_cursor_values` | 读取光标测量值 |

### 数学
| 工具 | 说明 |
|------|------|
| `configure_math` | 配置数学通道（加减乘除/FFT/微分/积分/平方根/绝对值） |

### 其他
| 工具 | 说明 |
|------|------|
| `autoset` | 自动设置 |
| `save_setup` / `recall_setup` | 保存/调用设置（内部存储或 USB） |
| `get_frequency_counter` | 读取硬件频率计 |
| `get_status` | 获取运行状态 |
| `get_next_error` | 读取并清除错误队列 |

## 使用示例

连接并获取设备信息：
```
> connect host=192.168.1.100
Connected to 192.168.1.100:5024

> get_id
Siglent Technologies,SDS2504X Plus,SDS2XJAC1R0021,1.5.2.10
```

测量通道 1 的峰峰值和频率：
```
> measure measurement=VPP source1=C1
VPP(C1) = 3.32 V

> measure measurement=FREQ source1=C1
FREQ(C1) = 1000.5 Hz
```

配置触发并获取波形数据：
```
> configure_trigger mode=NORMal source=C1 level=1.5
Trigger configured

> get_waveform_data source=C1
Waveform Data for C1:
  Total points in memory: 100000
  Points returned: 100000
  Min: -1.65 V
  Max: 1.68 V
  Mean: 0.01 V
```

配置通道并启用 FFT：
```
> configure_channel channel=1 display=true scale=0.5 coupling=D1M
Channel 1 configured:
  - Display: ON
  - Scale: 0.5 V/div
  - Coupling: D1M

> configure_math display=true function=FFT source1=C1 fft_window=HANN fft_scale=DBVRMS
Math configured
```

## 项目结构

```
oscilloscope-mcp/
├── server.py            # MCP Server 主程序
├── requirements.txt     # Python 依赖
├── .mcp.json            # MCP 配置模板
├── README.md            # 本文件
├── .gitignore
└── oscilloscope_mcp.log # 运行日志（自动生成）
```

## 兼容性

默认适配 **Siglent SDS2000X Plus**，但大多数 SCPI 命令与以下系列兼容：
- SDS1000X / SDS1000X-E
- SDS2000X / SDS2000X Plus / SDS2000X HD
- SDS5000X
- SDS6000A

## 日志

运行日志写入同目录下的 `oscilloscope_mcp.log`。

## 故障排除

| 问题 | 解决方案 |
|------|----------|
| 连接超时 | 检查示波器 IP 是否正确、网络是否互通 |
| 命令无响应 | 确认示波器端口 5024 未被防火墙阻止 |
| 通道不可用 | 2 通道型号只有 CH1-CH2 |
| MCP 启动失败 | `pip install -r requirements.txt` 确保依赖已安装 |

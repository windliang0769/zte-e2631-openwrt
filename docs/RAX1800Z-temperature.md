# RAX1800-Z 温度功能：来源、实现与验证

状态概览页「系统」表里的「温度信息」一行由两个包组成，二者都已进 `seed/e2631.config`
和 `Device/cmcc_rax1800-z` 的 `DEVICE_PACKAGES`：

| 包 | 位置 | 职责 |
|---|---|---|
| `kmod-rax1800z-thermal` | `overlay/package/kernel/rax1800z-thermal/` | 内核模块，绑定 `temp@94100034`，注册 hwmon `temp1_input` |
| `luci-app-rax1800z-temp` | `overlay/package/luci/luci-app-rax1800z-temp/` | rpcd 只读聚合 + 状态页 include，显示 `CPU: ..°C, WiFi0: ..°C` |

## 为什么需要自己写驱动

Linux 6.18 主线没有 `zxic,tempsensor` 的驱动。2026-11-05 提交主线的 ZTE ZX279128S
PHY 驱动 commit 里也明确写了「需要 SoC 温度传感器驱动」。但原厂固件自带
`drivers/tempsensor/temp_zx.c`，而本项目有整片 NAND 备份 —— 于是从原厂
Linux 4.1.25 vmlinux 反汇编还原了寄存器、采样时序和换算公式，再在真机上验证。

主板线 `drivers/thermal/zx2967_thermal.c` **不是同一个 IP**（寄存器窗口 0x14
字节、依赖 DT `thermal-zones` 的 slope/offset），不能 backport。2026-10-10 的
ZX279128S RFC 系列虽写 "thermal sensors operational"，24 个补丁里没有任何
thermal 驱动。

## 寄存器与算法

物理窗口 `0x94100034`，长度 `0x0c`：

| 偏移 | 用途 | 关键位 |
|---|---|---|
| `+0x00` | cfg | `BIT(12)` 转换脉冲、`BIT(13)` 探测器选择、`BIT(16)/BIT(17)` probe 清零 |
| `+0x04` | status | `BIT(12)` READY、`bits[9:0]` ADC |
| `+0x08` | 阈值 | probe 写 `0x3FFFFFFF` |

probe：清 BIT16/17 → 写阈值 → 清 BIT(12) → 置 BIT(13) → 等第一个非零 ADC。

每次换算（取 10 个非零样本的平均）：

```
t = (1550 * avg) >> 10
x = t - 1104
1139 <= t <= 1148 : T = x - (t-1139)/2
1149 <= t <= 1168 : T = (t-1109) - (t-1149)/4
x > 64            : T = x - 9
否则              : T = x
T > 100 压缩；T > 110 封顶
```

**实现要点**：原厂采样函数（`0xC02CE8E4`）在四步脉冲的每一步都重新读 CFG 再
`bic`/`orr`，是累积修改。若把函数开头的 CFG 快照用于全部四次写，`BIT(13)`
会在第二步被提前恢复，序列就错了。驱动里的 `ts_cfg_change()` 每次重读。

## 真机验证（2026-11-11，原厂 B 槽救援 shell）

- `/proc/tempsensor`：`temper value:37` → 预热后 `38`
- `devmem 0x94100038`：`BIT12`=READY 恒 1，低 10 位 ADC 恒 `0x2F4`=756
- `0x94100034` = `0x04002000`，`0x9410003c` = `0x3FFFFFFF`
- dmesg：`work clock 0`（无需额外开 CRM 时钟）、`bsp_temp_start,val = 0x2f3`
- 逆向公式 `convert(755)=37`、`convert(756)=38`，与原厂读数逐度吻合
- `echo "select=1" > /proc/tempsensor` 只切换探测器组，温度不变 → **板上只有一个
  SoC 温度源**，三个 selector 不是三个传感器

原厂 U-Boot 会把 `versioninfo` 环境变量拼进内核命令行，`init=/bin/sh` 时那些
token 会被当成 sh 的脚本参数导致 kernel panic；进救援 shell 前先 `setenv versioninfo`。

## 真机验收清单（v10 RAM 试跑时用）

```sh
# 模块与 DT 绑定
lsmod | grep rax1800z
dmesg | grep -i 'zxic\|tempsensor\|sensor up'

# hwmon 节点：name 必须是 zxic_tsensor，rpcd 靠 *zxic* 认 CPU
for h in /sys/class/hwmon/hwmon*; do printf '%s -> ' "$h"; cat "$h/name"; done
grep . /sys/class/hwmon/hwmon*/temp1_input      # 毫摄氏度，约 37000..40000

# 驱动自带的 ADC 调试口（platform device 上）
find /sys/devices -name adc_raw -exec sh -c 'echo "$1: $(cat "$1")"' _ {} \;

# 后端与页面
ubus call rax1800z_temp temps                  # {"cpu":38,"wifi":[],"wifi_count":0}
```

`WiFi0/WiFi1` 走另一条独立的路：主线 mt76 的 `mt7915/init.c` 原生注册 hwmon
`temp1_input`，前提是 WiFi caldata 与驱动起来；`kmod-hwmon-core` 已选中。
在那之前 WiFi 显示「不可用」是**诚实反映硬件状态**，不伪造数值。

## 安全边界

驱动只读/写 `0x94100034..0x9410003f` 这 12 字节，不碰 CRM 其它位、不碰 NAND、
不写环境变量。模块驻留内存根文件系统，断电即失效；原厂 B 槽保持完好。

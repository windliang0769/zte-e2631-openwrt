// SPDX-License-Identifier: GPL-2.0-only
/*
 * rax1800z_thermal.c -- ZXIC ZX279128R/128S on-die temperature sensor (hwmon)
 *
 * Hardware: CMCC RAX1800-Z. Register window 0x94100034, length 0x0c.
 * DT binding: temp@94100034 { compatible = "zxic,tempsensor"; reg = <...>; };
 *
 * Everything below was recovered by disassembling the OEM Linux 4.1.25
 * kernel shipped in the board's NAND backup (drivers/tempsensor/temp_zx.c):
 *   - temp_ctrl_read()       0xC02CE87C (returns cached degrees C)
 *   - 2 s sampling function  0xC02CE8E4
 *   - probe / init           0xC02CF2E0, bsp_temp_start 0xC02CF24C
 *   - /proc/tempsensor       prints "temper value:%d"
 * and then VERIFIED ON HARDWARE in the OEM rescue shell:
 *   - /proc/tempsensor -> temper value:37 (ADC 755), then 38 (ADC 756)
 *   - 0x94100034 cfg = 0x04002000, 0x94100038 status = ...2F4 (READY 1,
 *     ADC 756), 0x9410003c threshold = 0x3FFFFFFF
 *   - convert(755) = 37, convert(756) = 38 -- exactly the OEM readout.
 *   - dmesg "work clock 0": no extra CRM clock gating is needed.
 *
 * The sampling loop is a byte-for-byte behavioural port of 0xC02CE8E4.
 * IMPORTANT: the OEM code re-reads CFG before EVERY bit change (bic/orr on
 * the live register), so each step must be cumulative. Using a snapshot of
 * CFG for all four writes would restore BIT(13) too early and is wrong.
 *
 * Interrupts (51/90/91 in the OEM DT) are not used: this driver only polls.
 * The hwmon name "zxic_tsensor" is already matched by the is_cpu_label()
 * filter in luci-app-rax1800z-temp, so the status-overview row needs no edit.
 */

#include <linux/bits.h>
#include <linux/delay.h>
#include <linux/device.h>
#include <linux/err.h>
#include <linux/errno.h>
#include <linux/gfp.h>
#include <linux/hwmon.h>
#include <linux/io.h>
#include <linux/mod_devicetable.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/sysfs.h>
#include <linux/types.h>

#define TS_CFG			0x00
#define TS_STATUS		0x04
#define TS_THRESHOLD		0x08

#define CFG_CONV_PULSE		BIT(12)
#define CFG_DETECTOR_SEL	BIT(13)
#define CFG_INIT_CLEAR		(BIT(16) | BIT(17))
#define STATUS_READY		BIT(12)
#define ADC_MASK		GENMASK(9, 0)

#define TS_SAMPLES		10
#define TS_PULSE_DELAY_US	100
#define TS_SAMPLE_DELAY_US	50
#define TS_INIT_TIMEOUT		1000	/* iterations */
#define TS_READY_TIMEOUT	1000	/* iterations */

struct zx_ts_priv {
	void __iomem *base;
	struct mutex lock;
};

/* ------------------------------------------------------------------ sampling */

static u32 ts_status(struct zx_ts_priv *p)
{
	return readl(p->base + TS_STATUS);
}

static u32 ts_adc(struct zx_ts_priv *p)
{
	return ts_status(p) & ADC_MASK;
}

/*
 * One CFG bit change, exactly like the OEM "ldr r3,[r2]; bic/orr; str" triple.
 * The register is re-read every time so the four steps accumulate.
 */
static void ts_cfg_change(struct zx_ts_priv *p, u32 clear, u32 set)
{
	u32 v = readl(p->base + TS_CFG);

	v &= ~clear;
	v |= set;
	writel(v, p->base + TS_CFG);
	udelay(TS_PULSE_DELAY_US);
}

static int ts_wait_ready(struct zx_ts_priv *p)
{
	int i;

	for (i = 0; i < TS_READY_TIMEOUT; i++) {
		if (ts_status(p) & STATUS_READY)
			return 0;
		udelay(TS_SAMPLE_DELAY_US);
	}
	return -ETIMEDOUT;
}

/* OEM 0xC02CE908..0xC02CE984: clear BIT13, pulse BIT12 up/down, set BIT13. */
static u32 ts_one_conversion(struct zx_ts_priv *p)
{
	ts_cfg_change(p, CFG_DETECTOR_SEL, 0);
	ts_cfg_change(p, 0, CFG_CONV_PULSE);
	ts_cfg_change(p, CFG_CONV_PULSE, 0);
	ts_cfg_change(p, 0, CFG_DETECTOR_SEL);

	return ts_adc(p);
}

static u32 ts_raw_sample(struct zx_ts_priv *p)
{
	unsigned int sum = 0, n = 0, guard = 0;
	u32 adc;

	if (ts_wait_ready(p))
		return 0;

	while (n < TS_SAMPLES && guard++ < TS_SAMPLES * 20) {
		adc = ts_one_conversion(p);
		if (adc == 0)
			continue;
		sum += adc;
		n++;
		udelay(TS_SAMPLE_DELAY_US);
	}

	return n == TS_SAMPLES ? sum / TS_SAMPLES : 0;
}

/* ------------------------------------------------------- OEM conversion math */

/*
 * 0xC02CE9A4..0xC02CEA40. Verified against /proc/tempsensor on hardware:
 * convert(755)=37 and convert(756)=38. The two piecewise bands are the
 * vendor's non-linear calibration for roughly 35..56 C.
 */
static int oem_convert(u32 avg)
{
	u32 t = (1550 * avg) >> 10;
	int x = (int)t - 1104;
	int temp;

	if (t >= 1139 && t <= 1148)
		temp = x - (int)((t - 1139) / 2);
	else if (t >= 1149 && t <= 1168)
		temp = (int)t - 1109 - (int)((t - 1149) / 4);
	else if (x > 64)
		temp = x - 9;
	else
		temp = x;

	/* OEM clamp: compress readings above 100 C, hard cap at 110 C. */
	if (temp > 100)
		temp = 100 + (temp - 100) / 2;
	if (temp > 110)
		temp = 110;

	return temp;
}

/* -------------------------------------------------------------------- hwmon */

static umode_t zx_ts_is_visible(const void *drvdata,
				enum hwmon_sensor_types type, u32 attr, int ch)
{
	if (type == hwmon_temp && attr == hwmon_temp_input)
		return 0444;
	return 0;
}

static int zx_ts_read(struct device *dev, enum hwmon_sensor_types type,
		      u32 attr, int ch, long *val)
{
	struct zx_ts_priv *p = dev_get_drvdata(dev);
	u32 avg;
	int temp;

	if (type != hwmon_temp || attr != hwmon_temp_input)
		return -EOPNOTSUPP;

	mutex_lock(&p->lock);
	avg = ts_raw_sample(p);
	mutex_unlock(&p->lock);

	if (!avg)
		return -EIO;

	temp = oem_convert(avg);
	/* A nonsense ADC must never leak a plausible-but-fake number to LuCI. */
	if (temp < -40 || temp > 125)
		return -EIO;

	*val = (long)temp * 1000;	/* hwmon wants millidegree Celsius */
	return 0;
}

static const struct hwmon_ops zx_ts_hwmon_ops = {
	.is_visible = zx_ts_is_visible,
	.read = zx_ts_read,
};

static const struct hwmon_channel_info *const zx_ts_info[] = {
	HWMON_CHANNEL_INFO(temp, HWMON_T_INPUT),
	NULL
};

static const struct hwmon_chip_info zx_ts_chip_info = {
	.ops = &zx_ts_hwmon_ops,
	.info = zx_ts_info,
};

/* ------------------------------------------------- raw ADC debug attribute */

static ssize_t adc_raw_show(struct device *dev, struct device_attribute *a,
			    char *buf)
{
	struct zx_ts_priv *p = dev_get_drvdata(dev);
	u32 avg;

	mutex_lock(&p->lock);
	avg = ts_raw_sample(p);
	mutex_unlock(&p->lock);

	return sysfs_emit(buf, "%u\n", avg);
}
static DEVICE_ATTR_RO(adc_raw);

/* ------------------------------------------------------------------- probe */

static int zx_ts_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct zx_ts_priv *p;
	struct device *hwmon;
	int i, ret;

	p = devm_kzalloc(dev, sizeof(*p), GFP_KERNEL);
	if (!p)
		return -ENOMEM;

	p->base = devm_platform_ioremap_resource(pdev, 0);
	if (IS_ERR(p->base))
		return PTR_ERR(p->base);

	mutex_init(&p->lock);
	platform_set_drvdata(pdev, p);

	/* OEM probe 0xC02CF2E0: clear bits 16/17, program the third register,
	 * clear BIT(12), set BIT(13), then wait for the first non-zero code. */
	ts_cfg_change(p, CFG_INIT_CLEAR, 0);
	writel(0x3fffffff, p->base + TS_THRESHOLD);
	ts_cfg_change(p, CFG_CONV_PULSE, 0);
	ts_cfg_change(p, 0, CFG_DETECTOR_SEL);

	for (i = 0; i < TS_INIT_TIMEOUT; i++) {
		if (ts_adc(p))
			break;
		udelay(TS_PULSE_DELAY_US);
	}
	if (i == TS_INIT_TIMEOUT)
		dev_warn(dev, "sensor produced no ADC code; check CRM clock gating\n");
	else
		dev_info(dev, "sensor up, first ADC code %u\n", ts_adc(p));

	hwmon = devm_hwmon_device_register_with_info(dev, "zxic_tsensor", p,
						     &zx_ts_chip_info, NULL);
	if (IS_ERR(hwmon))
		return PTR_ERR(hwmon);

	ret = device_create_file(dev, &dev_attr_adc_raw);
	if (ret)
		dev_warn(dev, "adc_raw attribute not created (%d)\n", ret);

	return 0;
}

static void zx_ts_remove(struct platform_device *pdev)
{
	device_remove_file(&pdev->dev, &dev_attr_adc_raw);
}

static const struct of_device_id zx_ts_of_match[] = {
	{ .compatible = "zxic,tempsensor" },
	{ }
};
MODULE_DEVICE_TABLE(of, zx_ts_of_match);

static struct platform_driver zx_ts_driver = {
	.probe = zx_ts_probe,
	.remove = zx_ts_remove,
	.driver = {
		.name = "zxic-tempsensor",
		.of_match_table = zx_ts_of_match,
	},
};
module_platform_driver(zx_ts_driver);

MODULE_AUTHOR("RAX1800Z port");
MODULE_DESCRIPTION("ZXIC ZX279128 on-die temperature sensor (OEM algorithm)");
MODULE_LICENSE("GPL v2");

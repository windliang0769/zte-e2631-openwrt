# SPDX-License-Identifier: GPL-2.0-only
#
# kmod-rax1800z-thermal -- CMCC RAX1800-Z (ZX279128R) on-die temperature sensor.
#
# The register window, the sampling sequence and the conversion formula were
# recovered from the OEM Linux 4.1.25 kernel in the NAND backup and then
# VERIFIED ON HARDWARE against /proc/tempsensor (ADC 755 -> 37 C, 756 -> 38 C).
# See docs/RAX1800Z-temperature.md in this kit and the reverse-engineering
# report in the working repo (_dl/nand_backup/oem_probe/).
#
# This is a plain out-of-tree kmod package: no kernel patch, no Kconfig edit.
# The kernel already builds CONFIG_HWMON=m, and +kmod-hwmon-core pulls in
# hwmon.ko (which exports devm_hwmon_device_register_with_info) before this
# module is loaded at priority 60.
include $(TOPDIR)/rules.mk
include $(INCLUDE_DIR)/kernel.mk

PKG_NAME:=rax1800z-thermal
PKG_VERSION:=1.0.0
PKG_RELEASE:=1
PKG_LICENSE:=GPL-2.0-only

include $(INCLUDE_DIR)/package.mk

define KernelPackage/rax1800z-thermal
  SUBMENU:=Other modules
  TITLE:=CMCC RAX1800-Z SoC temperature sensor (hwmon)
  DEPENDS:=+kmod-hwmon-core
  FILES:=$(PKG_BUILD_DIR)/rax1800z_thermal.ko
  AUTOLOAD:=$(call AutoLoad,60,rax1800z_thermal,1)
  KCONFIG:=
endef

define KernelPackage/rax1800z-thermal/description
  ZXIC ZX279128R/128S on-die temperature sensor at 0x94100034.
  Registers a hwmon temp1_input (millidegree Celsius) plus an adc_raw debug
  attribute on the platform device. The hwmon name is zxic_tsensor, which the
  luci-app-rax1800z-temp rpcd backend already recognises as a CPU sensor.
  Read-only: no clock, reset, CRM or NAND registers are touched.
endef

# No Build/Prepare override: package-defaults.mk already copies ./src/. into
# $(PKG_BUILD_DIR) for a package with no PKG_SOURCE (same as button-hotplug).

define Build/Compile
	$(KERNEL_MAKE) M="$(PKG_BUILD_DIR)" modules
endef

$(eval $(call KernelPackage,rax1800z-thermal))

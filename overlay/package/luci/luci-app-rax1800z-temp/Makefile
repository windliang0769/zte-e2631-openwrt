# SPDX-License-Identifier: GPL-2.0-only
# RAX1800Z status overview: CPU / WiFi temperature line.
#
# Pure data, no compiled code: LUCI_PKGARCH=all like luci-app-e2631-wanport.
# The kernel side is deliberately untouched. CONFIG_HWMON is =m in this
# target and kmod-hwmon-core is selected by seed/e2631.config; mt7915/mt7916
# register temp1_input through devm_hwmon_device_register_with_groups() in
# mainline mt76. The SoC sensor (zxic,tempsensor @ 0x94100034) has NO upstream
# driver - see the ZX279128S PHY driver's own note that a temperature driver
# would be needed first - so the backend reports CPU as null, not a guess.
include $(TOPDIR)/rules.mk

LUCI_TITLE:=RAX1800Z board temperatures on the status overview
LUCI_DEPENDS:=+luci-base +luci-mod-status
LUCI_PKGARCH:=all

PKG_VERSION:=1.0.0
PKG_RELEASE:=1
PKG_PO_VERSION:=$(PKG_VERSION)-r$(PKG_RELEASE)
PKG_LICENSE:=GPL-2.0-only

include $(TOPDIR)/feeds/luci/luci.mk

# call BuildPackage - OpenWrt buildroot signature

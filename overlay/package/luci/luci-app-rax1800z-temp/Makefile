# SPDX-License-Identifier: GPL-2.0-only
# RAX1800Z status overview: CPU / WiFi temperature line.
#
# Pure data, no compiled code: LUCI_PKGARCH=all like luci-app-e2631-wanport.
# The kernel side lives in a separate out-of-tree kmod package
# (overlay/package/kernel/rax1800z-thermal). CONFIG_HWMON is =m in this
# target and kmod-hwmon-core is selected by seed/e2631.config; mt7915/mt7916
# register temp1_input through mainline mt76, while the SoC sensor
# (zxic,tempsensor @ 0x94100034) is driven by kmod-rax1800z-thermal and
# registers a hwmon device named zxic_tsensor. This rpcd backend only reads
# exported sysfs files - it never touches hardware registers itself.
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

# PDSSetup.tcl — 芯片项目全局配置 (示例)
# sync_owners.py 解析此文件中的 OWNER 映射更新到 config.yaml

# ---- 模块与 Owner 映射 ----
#set chip_owner(CPU,OWNER)       "w00949819"
#set chip_owner(DDR,OWNER)       "zhangsan"
set chip_owner(PCIE_CTRL,OWNER) "lisi"
set chip_owner(USB_PHY,OWNER)   "wangshuai"

# 不同的变量名前缀也能识别 (只要包含 OWNER 关键字)
set project_owner(DMA,OWNER)    "w00949819"
set block_owner(GPU_TOP,OWNER)  "lisi"

# 不带引号的值也能解析
set chip_owner(SPI,OWNER)       w00888888

# ---- 以下行应被忽略 (无 OWNER 关键字) ----
set chip_owner(CPU,TYPE)        "digital"
set chip_owner(DDR,AREA)        "1.2mm2"
set design_version               "v3.2"
set clock_freq                   2000

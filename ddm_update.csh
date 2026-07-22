#!/bin/csh -f
#=========================================================================
# DDM 项目更新脚本 (csh/tcsh) — 离线服务器版本
#
# 用法：
#   方法1: 指定新包路径（本地或 NFS 路径）
#     ddm_update.csh /path/to/ddm_v2.2.tar.gz
#
#   方法2: scp 从远程拉取
#     ddm_update.csh user@server:/path/to/ddm_v2.2.tar.gz
#
#   回退到上一个版本:
#     ddm_update.csh --rollback
#
#   列出已安装版本:
#     ddm_update.csh --list
#
# 部署目录结构:
#   ~/ddm                 → 软链接，指向生产版本
#   ~/ddm_home/           备份工作区
#     releases/            各版本归档
#     backups/             旧版本备份
#     current_config.yaml  当前配置备份
#=========================================================================

# ---- 配置 ----
set DDM_LINK   = "$HOME/ddm"                   # 生产软链接
set DDM_HOME   = "$HOME/ddm_home"               # 工作区
set RELEASES   = "$DDM_HOME/releases"           # 版本归档
set BACKUP_DIR = "$DDM_HOME/backups"            # 旧版本备份
set CONFIG_BAK = "$DDM_HOME/current_config.yaml" # 配置备份

# ---- 参数解析 ----
set method    = ""
set source    = ""
set rollback  = 0
set list_vers = 0
set force     = 0

if ($#argv == 0) goto usage

while ($#argv > 0)
    switch ($argv[1])
        case --rollback:
            set rollback = 1
            breaksw
        case --list:
            set list_vers = 1
            breaksw
        case --force:
            set force = 1
            breaksw
        case --help:
        case -h:
            goto usage
        default:
            set source = "$argv[1]"
            if ("$source" =~ *@*:*) then
                set method = "scp"
            else
                set method = "local"
            endif
            breaksw
    endsw
    shift
end

# ---- 初始化工作区 ----
if (! -d "$DDM_HOME") mkdir -p "$DDM_HOME"
if (! -d "$RELEASES")  mkdir -p "$RELEASES"
if (! -d "$BACKUP_DIR") mkdir -p "$BACKUP_DIR"

# ---- 列出已安装版本 ----
if ($list_vers) then
    echo ""
    echo "=== DDM 已安装版本 ==="
    if (-l "$DDM_LINK") then
        set current = `readlink "$DDM_LINK"`
        echo "  当前: $current"
    endif
    echo ""
    if (-d "$RELEASES") then
        ls -1dt "$RELEASES"/*/ | sed 's|.*/||;s|/$||' | while read v
            echo "    $v"
        end
    endif
    echo ""
    exit 0
endif

# ---- 回退 ----
if ($rollback) then
    if (! -d "$RELEASES") then
        echo "错误: 没有已安装的版本"
        exit 1
    endif
    set versions = `ls -1dt "$RELEASES"/*/ | sed 's|.*/||;s|/$||'`
    if ($#versions < 2) then
        echo "错误: 只有一个版本，无法回退"
        ls -1dt "$RELEASES"/*/
        exit 1
    endif
    set prev = "$versions[2]"
    echo ""
    echo "回退到: $prev"
    rm -f "$DDM_LINK"
    ln -sfn "$RELEASES/$prev" "$DDM_LINK"
    echo "完成。当前版本: $prev"
    python3 -m ddm check
    exit 0
endif

# ---- 部署 ----
set timestamp = `date +%Y%m%d_%H%M%S`
set tar_file  = "/tmp/ddm_update_${timestamp}.tar.gz"
set ver_dir   = ""

echo ""
echo "=== DDM 更新 ==="

# ---- Step 1: 获取压缩包 ----
if ("$method" == "scp") then
    echo "  从 $source 拉取..."
    scp "$source" "$tar_file"
    if ($status != 0) then
        echo "错误: scp 失败"
        exit 1
    endif
else if ("$method" == "local") then
    if (! -f "$source") then
        echo "错误: 文件不存在: $source"
        exit 1
    endif
    cp "$source" "$tar_file"
else
    goto usage
endif

# ---- Step 2: 检查压缩包 ----
if (! -f "$tar_file") then
    echo "错误: 压缩包不存在"
    exit 1
endif

set tar_size = `stat -c%s "$tar_file" 2>/dev/null || stat -f%z "$tar_file" 2>/dev/null`
if ($tar_size < 1024) then
    echo "错误: 压缩包太小 ($tar_size bytes)，可能已损坏"
    rm -f "$tar_file"
    exit 1
endif

# 提取版本名
set top_dir = `tar tzf "$tar_file" 2>/dev/null | head -1 | sed 's|/.*||'`
if ("$top_dir" == "") then
    echo "错误: 无法读取压缩包内容"
    rm -f "$tar_file"
    exit 1
endif

if ("$top_dir" =~ *_20*) then
    set ver_dir = "$top_dir"
else
    set ver_dir = "${top_dir}_${timestamp}"
endif

# ---- Step 3: 备份当前配置 ----
if (-l "$DDM_LINK" && -d "$DDM_LINK") then
    set current_config = "$DDM_LINK/config/config.yaml"
    if (-f "$current_config") then
        cp "$current_config" "$CONFIG_BAK"
        echo "  配置已备份: $CONFIG_BAK"
    endif
endif

# ---- Step 4: 备份当前版本 ----
if (-l "$DDM_LINK") then
    set current = `readlink "$DDM_LINK"`
    if (-d "$current") then
        set backup_name = `basename "$current"`
        echo "  备份当前版本: $backup_name"
        cp -r "$current" "$BACKUP_DIR/${backup_name}_${timestamp}"
    endif
endif

# ---- Step 5: 解压部署 ----
set deploy_dir = "$RELEASES/$ver_dir"
echo "  部署到: $deploy_dir"

if (-d "$deploy_dir") then
    if (! $force) then
        echo "错误: 版本 $ver_dir 已存在。使用 --force 覆盖。"
        rm -f "$tar_file"
        exit 1
    endif
    rm -rf "$deploy_dir"
endif

mkdir -p "$deploy_dir"
tar -xzf "$tar_file" -C "$deploy_dir" --strip-components=1
if ($status != 0) then
    echo "错误: 解压失败"
    rm -f "$tar_file"
    exit 1
endif
rm -f "$tar_file"

# ---- Step 5.5: 运行 install.sh (创建 venv, 装依赖, 设权限) ----
if (-f "$deploy_dir/install.sh") then
    echo "  运行 install.sh..."
    cd "$deploy_dir"
    bash install.sh
    if ($status != 0) then
        echo "  警告: install.sh 返回非零，请检查"
    endif
    cd -
endif

# ---- Step 6: 恢复配置 ----
if (-f "$CONFIG_BAK") then
    if (! -f "$deploy_dir/config/config.yaml") then
        cp "$CONFIG_BAK" "$deploy_dir/config/config.yaml"
        echo "  已恢复配置"
    else
        echo "  保留新包中的 config.yaml (旧配置: $CONFIG_BAK)"
    endif
endif

# ---- Step 7: 原子切换 ----
if (-l "$DDM_LINK" || -e "$DDM_LINK") then
    rm -f "$DDM_LINK"
endif
ln -sfn "$deploy_dir" "$DDM_LINK"

echo ""
echo "=== 更新完成 ==="
echo "  当前版本: $ver_dir"
echo "  旧版本:   $BACKUP_DIR/"
echo ""

# ---- Step 8: 验证 ----
echo "--- ddm check ---"
python3 -m ddm check
if ($status == 0) then
    echo ""
    echo "✓ 验证通过"
else
    echo ""
    echo "✗ 验证失败！回退: ddm_update.csh --rollback"
endif

exit 0

# ---- 帮助 ----
usage:
    echo ""
    echo "DDM 项目更新脚本 (离线)"
    echo ""
    echo "用法:"
    echo "  $0 /path/to/ddm.tar.gz                 本地或 NFS 路径"
    echo "  $0 user@server:/path/to/ddm.tar.gz     从远程 SCP 拉取"
    echo "  $0 --rollback                          回退到上一个版本"
    echo "  $0 --list                              列出已安装版本"
    echo ""
    echo "选项:"
    echo "  --force    覆盖已存在的版本"
    echo ""
    echo "示例:"
    echo "  $0 /nfs/eda/packages/ddm_v2.2.tar.gz"
    echo "  $0 user@dev-server:/tmp/ddm_v2.2.tar.gz"
    echo "  $0 --rollback"
    echo ""
    exit 1

#!/bin/csh -f
#=========================================================================
# DDM 项目更新脚本 (csh/tcsh)
#
# 用法：
#   方法1: scp 从远程服务器拉取
#     ddm_update.csh user@server:/path/to/ddm_v2.2.tar.gz
#
#   方法2: 本地压缩包部署
#     ddm_update.csh ./ddm_v2.2.tar.gz
#
#   方法3: FileCodeBox 提取码下载
#     ddm_update.csh --code 57067
#
#   回退到上一个版本:
#     ddm_update.csh --rollback
#
#   列出已安装版本:
#     ddm_update.csh --list
#
# 部署目录结构:
#   ~/ddm                 → 软链接，指向生产版本 (所有用户 PYTHONPATH 指向这)
#   ~/ddm_home/            备份工作区
#     releases/v1/         各版本归档
#     current_config.yaml  当前配置备份
#=========================================================================

# ---- 配置 ----
set DDM_LINK   = "$HOME/ddm"                   # 生产软链接
set DDM_HOME   = "$HOME/ddm_home"               # 工作区
set RELEASES   = "$DDM_HOME/releases"           # 版本归档
set BACKUP_DIR = "$DDM_HOME/backups"            # 旧版本备份
set CONFIG_BAK = "$DDM_HOME/current_config.yaml" # 配置备份
set FILEBOX    = "https://filebox.a.wssss.org.cn"

# ---- 参数解析 ----
set method    = ""
set source    = ""
set code      = ""
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
        case --code:
            shift
            set code = "$argv[1]"
            set method = "filebox"
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
        echo "错误: 没有已安装的版本 (releases/ 目录不存在)"
        exit 1
    endif
    # 找出最近两个版本，回退到第二个（上一个）
    set versions = `ls -1dt "$RELEASES"/*/ | sed 's|.*/||;s|/$||'`
    if ($#versions < 2) then
        echo "错误: 只有一个版本，无法回退"
        ls -1dt "$RELEASES"/*/
        exit 1
    endif
    set prev = "$versions[2]"
    echo ""
    echo "回退到: $prev"
    echo ""
    rm -f "$DDM_LINK"
    ln -sfn "$RELEASES/$prev" "$DDM_LINK"
    echo "完成。当前版本: $prev"
    echo "  ddm check 验证:"
    python3 -m ddm check
    exit 0
endif

# ---- 部署 ----
set timestamp = `date +%Y%m%d_%H%M%S`
set tar_file  = "/tmp/ddm_update_${timestamp}.tar.gz"
set ver_dir   = ""

# ---- Step 1: 获取压缩包 ----
echo ""
echo "=== DDM 更新 ==="

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

else if ("$method" == "filebox") then
    echo "  从 FileCodeBox 下载 (code=$code)..."
    # FileCodeBox select API: get download URL
    set info = `curl -sL "$FILEBOX/share/select/?code=$code" | \
                python3 -c "
import sys, gzip
data = sys.stdin.buffer.read()
if data[:2] == b'\x1f\x8b':
    data = gzip.decompress(data)
print(data.decode('utf-8', errors='ignore')[:2000])
" 2>/dev/null`
    # Fallback: try the direct tar response (FileCodeBox might return raw)
    curl -sL "$FILEBOX/share/select/?code=$code" -o "$tar_file" 2>/dev/null
    if ($status != 0 || ! -f "$tar_file") then
        echo "错误: 下载失败"
        exit 1
    endif
    # Check if it's HTML (error page)
    set head = `head -c 100 "$tar_file"`
    if ("$head" =~ *\<html* || "$head" =~ *Error*) then
        echo "错误: 提取码无效或文件已过期"
        rm -f "$tar_file"
        exit 1
    endif

else
    goto usage
endif

# ---- Step 2: 检查压缩包完整性 ----
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

# 提取版本名（从压缩包顶层目录名推断）
set top_dir = `tar tzf "$tar_file" 2>/dev/null | head -1 | sed 's|/.*||'`
if ("$top_dir" == "") then
    echo "错误: 无法读取压缩包内容"
    rm -f "$tar_file"
    exit 1
endif

# 如果顶层目录名不包含日期，加上时间戳
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

# ---- Step 6: 恢复配置 ----
if (-f "$CONFIG_BAK") then
    if (! -f "$deploy_dir/config/config.yaml") then
        cp "$CONFIG_BAK" "$deploy_dir/config/config.yaml"
        echo "  已恢复配置"
    else
        echo "  保留压缩包中的 config.yaml (如需恢复旧配置: cp $CONFIG_BAK $deploy_dir/config/config.yaml)"
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
    echo "✗ 验证失败！回退:"
    echo "  ln -sfn $BACKUP_DIR/<旧版本> $DDM_LINK"
endif

exit 0

# ---- 帮助 ----
usage:
    echo ""
    echo "DDM 项目更新脚本"
    echo ""
    echo "用法:"
    echo "  $0 user@server:/path/to/ddm.tar.gz    从远程服务器 SCP 拉取"
    echo "  $0 ./ddm.tar.gz                       本地压缩包部署"
    echo "  $0 --code EXTRACTION_CODE             从 FileCodeBox 下载"
    echo "  $0 --rollback                         回退到上一个版本"
    echo "  $0 --list                             列出已安装版本"
    echo ""
    echo "选项:"
    echo "  --force    覆盖已存在的版本"
    echo "  --help     显示此帮助"
    echo ""
    echo "示例:"
    echo "  $0 user@dev-server:/tmp/ddm_v2.2.tar.gz"
    echo "  $0 --rollback"
    echo ""
    exit 1

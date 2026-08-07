#!/bin/csh -f
#=========================================================================
# DDM 项目更新脚本 (csh/tcsh) — 离线服务器版本
#
# 用法：
#   方法1: 指定新包路径（本地或 NFS 路径）
#     ddm_update.csh /path/to/ddm_v2.2.tar.gz
#     ddm_update.csh --dir /nfs/eda/shared/ddm /path/to/ddm_v2.2.tar.gz
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
#   清理旧版本 (保留最近 N 个):
#     ddm_update.csh --clean [N]
#
# 部署目录结构:
#   $INSTALL_DIR          → 软链接，指向生产版本 (默认 ~/ddm)
#   $(dirname $INSTALL_DIR)/.ddm/
#     releases/            各版本归档 (回退从这里取)
#     current_config.yaml  当前配置备份
#=========================================================================

# ---- 配置 ----
set INSTALL_DIR = "$HOME/ddm"                   # 默认安装目录

# ---- 参数解析 ----
set method    = ""
set source    = ""
set rollback  = 0
set list_vers = 0
set force     = 0
set clean_n   = 0

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
        case --clean:
            shift
            if ($#argv > 0) then
                # Check if argument looks like a number
                set _n = `echo "$argv[1]" | grep -c '^[0-9][0-9]*$'`
                if ($_n == 1) then
                    set clean_n = "$argv[1]"
                else
                    set clean_n = 3
                    continue
                endif
            else
                set clean_n = 3
                continue
            endif
            breaksw
        case --dir:
            shift
            set INSTALL_DIR = "$argv[1]"
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

# Resolve INSTALL_DIR to an absolute path WITHOUT following the
# final symlink (unlike readlink -f).  "source" in .cshrc must
# reference the stable link, not the versioned target directory.
set INSTALL_DIR = `python3 -c "import os; print(os.path.abspath('$INSTALL_DIR'))"`

# 管理数据放在 install_dir 同级 .ddm/ 下
# 如果 dirname 是 / (root)，改用 /tmp/.ddm/
set DDM_LINK   = "$INSTALL_DIR"
set _parent = `dirname "$INSTALL_DIR"`
if ("$_parent" == "/") then
    set DDM_MGMT = "/tmp/.ddm"
else
    set DDM_MGMT = "$_parent/.ddm"
endif
set RELEASES   = "$DDM_MGMT/releases"            # 版本归档
set CONFIG_BAK = "$DDM_MGMT/current_config.yaml"  # 配置备份

# ---- 初始化工作区 ----
if (! -d "$DDM_MGMT") mkdir -p "$DDM_MGMT"
if (! -d "$RELEASES")  mkdir -p "$RELEASES"

# ---- 列出已安装版本 ----
if ($list_vers) then
    echo ""
    echo "=== DDM 已安装版本 ==="
    if (-l "$DDM_LINK") then
        set current = `basename \`readlink "$DDM_LINK"\``
        echo "  当前: $current"
    endif
    echo ""
    if (-d "$RELEASES") then
        set count = 0
        foreach v (`ls -1dt "$RELEASES"/*/`)
            @ count++
            set vname = `basename "$v"`
            if ( -l "$DDM_LINK" && `readlink "$DDM_LINK"` == "$v" ) then
                echo "  * $vname  ← current"
            else
                echo "    $vname"
            endif
        end
        echo ""
        echo "  共 $count 个版本"
    endif
    echo ""
    exit 0
endif

# ---- 清理旧版本 ----
if ($clean_n > 0) then
    if (! -d "$RELEASES") exit 0
    set all = `ls -1dt "$RELEASES"/*/`
    if ($#all <= $clean_n) then
        echo "只有 $#all 个版本，无需清理 (保留 $clean_n 个)"
        exit 0
    endif
    set keep = "$clean_n"
    set deleted = 0
    foreach v ($all)
        @ keep--
        if ($keep >= 0) continue
        set vname = `basename "$v"`
        # Never delete the currently active version
        if (-l "$DDM_LINK" && `readlink "$DDM_LINK"` == "$v") continue
        echo "  删除: $vname"
        rm -rf "$v"
        @ deleted++
    end
    echo ""
    echo "已清理 $deleted 个旧版本 (保留最近 $clean_n 个)"
    exit 0
endif

# ---- 回退 ----
if ($rollback) then
    if (! -d "$RELEASES") then
        echo "错误: 没有已安装的版本"
        exit 1
    endif
    set versions = `ls -1dt "$RELEASES"/*/`
    if ($#versions < 2) then
        echo "错误: 只有一个版本，无法回退"
        exit 1
    endif

    # Skip current version, pick the previous one
    set current = ""
    if (-l "$DDM_LINK") set current = `readlink "$DDM_LINK"`
    set prev = ""
    foreach v ($versions)
        if ("$v" != "$current" && "$prev" == "") then
            set prev = "$v"
            break
        endif
    end
    if ("$prev" == "") then
        echo "错误: 找不到可回退的版本"
        exit 1
    endif

    set prev_name = `basename "$prev"`
    echo ""
    echo "回退: `basename \"$current\"` → $prev_name"
    rm -f "$DDM_LINK"
    ln -sfn "$prev" "$DDM_LINK"
    echo "完成。当前版本: $prev_name"
    if (-x "$DDM_LINK/venv/bin/python3") then
        "$DDM_LINK/venv/bin/python3" -m ddm check
    else
        python3 -m ddm check
    endif
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

set tar_size = `python3 -c "import os; print(os.path.getsize('$tar_file'))"`
if ($tar_size < 1024) then
    echo "错误: 压缩包太小 (${tar_size} bytes)，可能已损坏"
    rm -f "$tar_file"
    exit 1
endif

# 提取版本名
set top_dir = `tar tzf "$tar_file" | head -1 | sed 's|/.*||'`
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

# ---- Step 4: 解压部署 ----
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

# ---- Step 5: 运行 install.sh (创建 venv, 装依赖, 设权限) ----
# Pass DDM_LINK so setup_user.sh embeds the *stable* symlink path,
# not the versioned directory.  Users won't need to re-run setup after
# an update — the symlink target changes but the path in .cshrc stays.
if (-f "$deploy_dir/install.sh") then
    echo "  运行 install.sh..."
    cd "$deploy_dir"
    setenv DDM_LINK "$INSTALL_DIR"
    bash install.sh
    unsetenv DDM_LINK
    if ($status != 0) then
        echo "  警告: install.sh 返回非零，请检查"
    endif
    cd -
endif

# ---- Step 6: 恢复配置 ----
# Always restore the previous config — deployments never ship a real config.yaml,
# only a config.yaml.example.
if (-f "$CONFIG_BAK") then
    cp "$CONFIG_BAK" "$deploy_dir/config/config.yaml"
    echo ""
    echo "  已恢复配置"
    if (-f "$deploy_dir/config/config.yaml.example") then
        echo "  [提示] 新版本可能有新增配置项，请对比:"
        echo "    diff $deploy_dir/config/config.yaml $deploy_dir/config/config.yaml.example"
    endif
else
    echo ""
    echo "  首次安装: 请编辑 config/config.yaml"
endif

# ---- Step 7: 原子切换 ----
ln -sfn "$deploy_dir" "$DDM_LINK"

# ---- Step 8: 清理旧版本 (保留最近 5 个) ----
if (-d "$RELEASES") then
    set keep_n = 5
    set all = `ls -1dt "$RELEASES"/*/`
    if ($#all > $keep_n) then
        set keep = "$keep_n"
        set cleaned = 0
        foreach v ($all)
            @ keep--
            if ($keep >= 0) continue
            if (-l "$DDM_LINK" && `readlink "$DDM_LINK"` == "$v") continue
            rm -rf "$v"
            @ cleaned++
        end
        if ($cleaned > 0) echo "  已清理 $cleaned 个旧版本 (保留最近 $keep_n 个)"
    endif
endif

echo ""
echo "=== 更新完成 ==="
echo "  当前版本: $ver_dir"
echo "  部署路径: $DDM_LINK"
echo ""

# ---- Step 9: 验证 ----
echo "--- ddm check ---"
if (-x "$deploy_dir/venv/bin/python3") then
    "$deploy_dir/venv/bin/python3" -m ddm check
else
    python3 -m ddm check
endif
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
    echo "  $0 --clean [N]                         清理旧版本 (保留最近 N 个, 默认 3)"
    echo ""
    echo "选项:"
    echo "  --dir PATH    安装目录 (默认: ~/ddm)"
    echo "  --force       覆盖已存在的版本"
    echo ""
    echo "示例:"
    echo "  $0 /path/to/ddm_v0.6.0.tar.gz"
    echo "  $0 --dir /nfs/eda/shared/ddm /path/to/ddm.tar.gz"
    echo "  $0 --rollback"
    echo "  $0 --list"
    echo "  $0 --clean 3"
    echo ""
    exit 1

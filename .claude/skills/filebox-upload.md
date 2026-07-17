# FileCodeBox 文件上传

将项目文件打包上传到私有 FileCodeBox 站点，获取提取码和下载链接。

## 默认配置

| 配置项 | 值 |
|--------|-----|
| 站点地址 | `https://filebox.a.wssss.org.cn` |
| 管理密码 | `winsight2` |
| 过期方式 | `count`（按次） |
| 过期次数 | `10` |
| 排除项 | `.git` `__pycache__` `*.pyc` `repository` `logs` `dist` `build` `*.egg-info` `.pytest_cache` `node_modules` |

## 流程

### Step 1: 打包

```bash
tar -czf /tmp/<name>.tar.gz \
  -C <project_parent_dir> \
  --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='repository' --exclude='logs' --exclude='dist' \
  --exclude='build' --exclude='*.egg-info' --exclude='.pytest_cache' \
  --exclude='node_modules' \
  <project_dir_name>
```

### Step 2: 登录获取 Token

```bash
TOKEN=$(curl -s -X POST "<SITE>/admin/login" \
  -H "Content-Type: application/json" \
  -d '{"password":"<PASSWORD>"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['detail']['token'])")
```

如果返回 `code: 428`（系统未初始化），先执行初始化：

```bash
curl -s -X POST "<SITE>/setup" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d 'site_name=文件快递柜&admin_password=<PASSWORD>&confirm_password=<PASSWORD>&upload_size_value=100&upload_size_unit=MB&uploadCount=10&uploadMinute=1&openUpload=1&errorCount=10&errorMinute=1&save_time_value=0&save_time_unit=day&code_generate_type=secret&expireStyle=day&expireStyle=hour&expireStyle=forever&expireStyle=count&allowed_file_types=*'
```

### Step 3: 初始化上传

```bash
INIT=$(curl -s -X POST "<SITE>/presign/upload/init" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"file_name\":\"<filename>\",\"file_size\":$(stat -c%s /tmp/<name>.tar.gz),\"expire_value\":10,\"expire_style\":\"count\"}")
UPLOAD_ID=$(echo "$INIT" | python3 -c "import sys,json; print(json.load(sys.stdin)['detail']['upload_id'])")
```

### Step 4: 上传文件

```bash
curl -s -X PUT "<SITE>/presign/upload/proxy/${UPLOAD_ID}" \
  -H "Authorization: Bearer ${TOKEN}" \
  -F "file=@/tmp/<name>.tar.gz"
```

返回 `{"code":200,...,"detail":{"code":"<提取码>","name":"<文件名>"}}`

### Step 5: 确认上传

```bash
curl -s -X POST "<SITE>/presign/upload/confirm/${UPLOAD_ID}" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"expire_value":10,"expire_style":"count"}'
```

### Step 6: 输出结果

```
上传成功！
  提取码: <CODE>
  文件名: <NAME>
  下载:   <SITE>/share/select/?code=<CODE>
```

## 注意事项

- 上传失败时检查站点是否可访问，必要时提示用户检查服务器状态
- 如果 `INIT` 返回 `upload_id` 提取失败，检查返回 JSON 的 `code` 字段，如为 428 则需初始化
- `PUT /share/file/` 是旧版接口，不要使用；始终走 presign 流程
- 打包时默认从项目父目录打包，保留项目目录名作为归档顶层

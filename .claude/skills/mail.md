# QQ 邮箱收发邮件

通过 QQ 邮箱 SMTP/IMAP 发送邮件和查看收件箱。

## 配置

| 配置项 | 值 |
|--------|-----|
| 发件人 | wssssorg@qq.com |
| SMTP | smtp.qq.com:587 (STARTTLS) |
| IMAP | imap.qq.com:993 (SSL) |
| 工具脚本 | .claude/skills/mail_util.py |

## 发送邮件

当用户要求发送邮件时，使用:

```bash
python3 .claude/skills/mail_util.py send <收件人邮箱> "<主题>" "<正文>"
```

例如:
```bash
python3 .claude/skills/mail_util.py send zhangsan@example.com "DDM Release V3" "PV_ITER V3 已发布。路径: /nfs/eda/ddm/release/PV_ITER/V3"
```

## 查看邮件

当用户要求查看邮件时，使用:

```bash
python3 .claude/skills/mail_util.py check 5     # 最近5封
python3 .claude/skills/mail_util.py check 10    # 最近10封
```

## 注意事项

- 使用授权码认证，无需密码
- 邮件以 "DDM System" 作为发件人显示名
- 查看邮件时显示发件人、主题、时间和正文摘要

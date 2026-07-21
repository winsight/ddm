# Mail — QQ 邮箱收发

通过 QQ 邮箱 SMTP/IMAP 发送邮件和查看收件箱。

## 用法

```
/mail send <to> <subject> <body>    发送邮件
/mail check [count]                  查看最新邮件 (默认 5 封)
```

## 实现

调用 `python3 /path/to/mail_util.py <action> [args...]`。

### 发送邮件

```bash
python3 mail_util.py send recipient@example.com "Subject Here" "Body text here"
```

### 查看邮件

```bash
python3 mail_util.py check 5     # 查看最近 5 封
python3 mail_util.py check 10    # 查看最近 10 封
```

## 注意事项

- QQ 邮箱使用授权码而非登录密码，已在代码中配置
- SMTP: smtp.qq.com:587 (STARTTLS)
- IMAP: imap.qq.com:993 (SSL)
- 发件地址: wssssorg@qq.com

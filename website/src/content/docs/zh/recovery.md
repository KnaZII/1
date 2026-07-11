---
title: IP 封锁恢复
description: 服务器 IP 被封锁后怎么办 — 诊断和恢复方案。
order: 7
section: guides
---

## 诊断

从本地机器运行（无需 SSH）：

```
meridian test IP
```

如果 TCP 端口 443 检查失败，IP 可能被您的 ISP 或政府封锁。这是受审查地区最常见的问题。

## 紧急缓解

如果您使用**域名模式**（`--domain`）部署，您的 WSS/CDN 连接仍然有效——它通过 Cloudflare 的 CDN 路由，完全绕过 IP 封锁。告诉用户切换到其连接页面上的 WSS 连接链接。

如果您已部署**中继**，通过中继连接的客户端不受影响——他们连接到中继的本地 IP，而不是被封锁的出站 IP。

## 恢复选项

### 选项 A：部署新服务器

如果客户端少且没有中继，这是最快的方式：

```bash
# 1. 从您的提供商获得新 VPS（新 IP）
# 2. 部署 Meridian
meridian deploy NEW_IP

# 3. 重新添加每个客户端
meridian client add alice --server NEW_IP
meridian client add bob --server NEW_IP

# 4. 向用户发送新的连接页面
```

部署是幂等的——在同一 IP 上重新运行是安全的，并从中断处继续。

### 选项 B：新出站服务器 + 现有中继

如果您已部署中继，这是最佳方案——您的客户端保留中继连接，同时交换其后的出站服务器：

```bash
# 1. 部署新出站服务器
meridian deploy NEW_EXIT_IP

# 2. 在新出站上重新添加客户端
meridian client add alice --server NEW_EXIT_IP
meridian client add bob --server NEW_EXIT_IP

# 3. 将中继切换到新出站
meridian relay remove RELAY_IP --exit OLD_EXIT_IP
meridian relay deploy RELAY_IP --exit NEW_EXIT_IP

# 客户端自动重新连接——中继 IP 不变
```

### 选项 C：添加域名模式以实现 CDN 回退

如果您之前未使用域名模式，现在添加它以防止未来中断：

```bash
meridian deploy NEW_IP --domain proxy.example.com
```

使用域名模式，即使服务器 IP 被封锁，WSS/CDN 连接也能工作——流量通过 Cloudflare 路由。有关 Cloudflare 设置的详细信息，请参阅[域名模式指南](/docs/zh/domain-mode/)。

## 主动防御

在 IP 被封锁**之前**设置弹性：

1. **部署中继**——为客户端提供本地入口点。当出站 IP 被封锁时，交换中继后的出站，无需触及客户端：
   ```bash
   meridian relay deploy RELAY_IP --exit EXIT_IP
   ```

2. **启用域名模式**——添加即使 IP 被封锁也能工作的 WSS/CDN 回退：
   ```bash
   meridian deploy EXIT_IP --domain proxy.example.com
   ```

3. **两者都使用**——最大弹性。客户端有三条路径：中继（本地）、CDN（Cloudflare）和直接（如果未被封锁）。

## 客户端迁移

每个客户端都必须在新服务器上手动重新添加——尚无自动迁移工具。工作流程：

1. 部署新服务器
2. 为每个客户端运行 `meridian client add NAME`
3. 与用户共享新的连接页面（二维码、可共享 URL 或 HTML 文件）

连接页面自动生成，包含所有可用的连接选项（直接、中继、CDN）。如果启用了服务器托管的页面，可共享的 URL 会自动更新。

## 保留旧服务器

不要立即关闭旧服务器——它可能在数天或数周后解封。您可以定期检查：

```bash
meridian test OLD_IP
```

如果恢复了，您有一个备用出站服务器准备就绪。

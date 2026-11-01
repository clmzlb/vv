# 签名密钥

应用 APK、内核清单和组件清单由同一把 RSA 私钥签名。仓库只保存对应公钥
`app/src/main/assets/cores/public-key.der`，私钥不进入版本库。

## 密钥存放

| 材料 | 位置 |
|---|---|
| Keystore（PKCS#12） | CI secret `ARCAENBOX_KEYSTORE`，值为 base64 编码的完整文件 |
| 口令 | CI secret `ARCAENBOX_KEYSTORE_PASSWORD` |
| Alias | `arcaenbox` |
| 公钥 | 仓库内 `app/src/main/assets/cores/public-key.der` |

`buildSrc/src/main/kotlin/Helpers.kt` 从 `local.properties` 或同名环境变量读取
`KEYSTORE_PASS`、`ALIAS_NAME`、`ALIAS_PASS`，并把 `release.keystore` 用作签名文件。
工作流在构建期间用 `printf '%s' "$SIGNING_KEYSTORE" | base64 --decode > release.keystore`
临时还原文件，任务结束后随 runner 销毁。

本地签名时把 keystore 放到仓库根的 `release.keystore`，并在 `local.properties`
写入上述三个属性。`.signing/` 与 `*.keystore` 均已被 `.gitignore` 排除。

## 离线备份

轮换期间在仓库内临时使用过 `.signing/` 存放密钥材料，该目录已被 `.gitignore`
排除，且已于 2026-11-02 从工作区删除。删除前的完整副本保存在
`%USERPROFILE%\Desktop\vv-signing-backup-2026-11-01\`：

| 文件 | SHA-256 |
|---|---|
| `release.keystore` | `7ca9738b0fe868f747f07f7d45f789d3f7458607472f96a76c6b47593b86e30a` |
| `keystore.b64` | `dafe6b789e3010ab26e6c28086760d14f516c2f627ad4fd36d6d4ac4d859c9a7` |
| `pub-new.der` | `e907fb38102c83c0a6091b534d6eb430e6b2b014f07a219d23cd78a56d67fd00` |

`pub-new.der` 与仓库内 `public-key.der` 字节一致。该目录是明文私钥，
应尽快迁移到密码管理器或加密卷，不要留在桌面、不要上传网盘或提交进仓库。

## 历史遗留

2026-11-01 之前 `release.keystore` 曾被提交到公开仓库（第一个提交 `7d9798c`）。
该文件已从当前树移除并作废，仓库历史中仍可取出，但对应私钥不再用于任何签名。
口令从未提交，因此不必假定旧私钥可用，但也不应继续信任它。

## 轮换记录

2026-11-01 首次轮换，起因即上述泄露。

- 新证书 `CN=ArcaenBox Release, O=ArcaenBox, C=CN`，RSA 4096，有效期至 2056-10-24。
- 证书 SHA-256 指纹：`258A264E4BD2783C85069AF7C09C1D7E388C31215DABB8342A52C674C7D89DF2`。
- 应用包名同时由 `com.arcaenbox.android` 改为 `com.vv.android`。

轮换是一次性的破坏性变更，原因如下：

1. Android 只允许同签名证书的 APK 覆盖安装。签名变更后，旧版本无法就地升级，
   必须卸载重装；卸载会清除节点、规则和设置。包名同时变更使得新旧可以共存，
   便于先导出配置再迁移。
2. 应用内置公钥随之更换，此前发布的内核与组件包全部无法通过新版本校验。
   所有渠道都需要用新密钥重新构建并递增 revision 后重新发布。

## 再次轮换的步骤

1. 生成新密钥：

   ```
   openssl req -x509 -newkey rsa:4096 -sha256 -days 10950 -nodes \
     -keyout key.pem -out cert.pem \
     -subj "/CN=ArcaenBox Release/O=ArcaenBox/C=CN" \
     -addext "basicConstraints=critical,CA:false" \
     -addext "keyUsage=critical,digitalSignature" \
     -addext "extendedKeyUsage=codeSigning"
   openssl pkcs12 -export -inkey key.pem -in cert.pem -name arcaenbox \
     -out release.keystore -passout "pass:<口令>"
   ```

2. 用公钥覆盖 `app/src/main/assets/cores/public-key.der`：

   ```
   openssl pkcs12 -in release.keystore -nokeys -clcerts -passin "pass:<口令>" \
     | openssl x509 -pubkey -noout \
     | openssl pkey -pubin -outform DER -out app/src/main/assets/cores/public-key.der
   ```

   长度应为 550 字节（RSA 4096，SubjectPublicKeyInfo）。

3. 更新 secret `ARCAENBOX_KEYSTORE` 为新文件的 base64，`ARCAENBOX_KEYSTORE_PASSWORD`
   为新口令。

4. 递增 `buildScript/builtins/versions.json` 中全部渠道的 `revision`，重新构建并发布。

5. 更新本文档的指纹与日期。

## 校验

`package_components.py` 与 `package_cores.py` 都会断言 keystore 的证书公钥等于仓库内
固定的 `public-key.der`，密钥与公钥不匹配时构建立即失败，不会产出应用无法验证的包。

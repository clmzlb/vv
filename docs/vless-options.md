# VLESS options and the bundled Xray core

The VLESS editor carries the same field set as v2rayNG, and every VLESS profile
runs on the bundled Xray. sing-box keeps the VPN interface, routing, DNS and
socket protection; Xray only owns the upstream connection.

## Fields

| Group | Fields |
| --- | --- |
| Identity | UUID, flow (`xtls-rprx-vision`), VLESS encryption (`none` or a `mlkem768x25519plus…` string) |
| Transport | `tcp`, `ws`, `http`, `quic`, `grpc`, `httpupgrade`, `xhttp`; TCP header type (`none`/`http`); XHTTP mode and `extra` JSON |
| TLS | SNI, ALPN, certificates, uTLS fingerprint, ECH |
| REALITY | public key, short id, `spiderX`, `mldsa65Verify` |
| Packet | packet encoding (packetaddr/xudp), mux |

`security=reality` is a first-class option. Profiles imported before this change
stored REALITY as `tls` plus a public key; they keep working and are still read
as REALITY.

`allow insecure` is gone. Xray 26.10 removed the field, so the editor no longer
offers it, links no longer carry `allowInsecure`, and stored profiles drop the
value on the next save. Only the global "Always allow insecure" master switch
still applies, and only to the sing-box protocols that support it.

## Backend

`usesXrayBackend()` returns true for every VLESS profile. Xray receives a
loopback SOCKS listener and reaches the server through sing-box's existing
mapping inbound, so the server address is never exposed to an unprotected
socket. VMess, Trojan and HTTP nodes are unchanged and stay on sing-box.

The Xray path rejects combinations Xray 26.10 cannot build: ECH, and the
removed `http` (h2) / `quic` transports. Multiplexing is passed to Xray as its
own `mux` block; packet encoding is a sing-box knob and is simply left
unexpressed (Xray always relays UDP as XUDP). REALITY is limited to `tcp`,
`xhttp` and `grpc`, matching Xray itself, and XTLS Vision cannot be combined
with multiplexing.

## Links

`vless://` links are read and written with the v2rayNG parameter names:
`type` (`raw` is normalised to `tcp`, `splithttp` to `xhttp`), `headerType`,
`mode`, `extra`, `security`, `sni`, `alpn`, `fp`, `pbk`, `sid`, `spx`,
`mldsa65Verify`, `flow`, `encryption`, `packetEncoding` and `fm`.

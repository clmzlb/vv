# VLESS options and the Xray fallback

The VLESS editor carries the same field set as v2rayNG. Most nodes still run on
sing-box; a node switches to the bundled Xray only when sing-box cannot express
it.

## Fields

| Group | Fields |
| --- | --- |
| Identity | UUID, flow (`xtls-rprx-vision`), VLESS encryption (`none` or a `mlkem768x25519plus…` string) |
| Transport | `tcp`, `ws`, `http`, `quic`, `grpc`, `httpupgrade`, `xhttp`; TCP header type (`none`/`http`); XHTTP mode and `extra` JSON |
| TLS | SNI, ALPN, certificates, allow insecure, uTLS fingerprint, ECH |
| REALITY | public key, short id, `spiderX`, `mldsa65Verify` |
| Packet | packet encoding (packetaddr/xudp), mux |

`security=reality` is a first-class option. Profiles imported before this change
stored REALITY as `tls` plus a public key; they keep working and are still read
as REALITY.

## When the bundled Xray runs the node

`usesXrayBackend()` is true for VLESS profiles that use any of:

- `type=xhttp` (Xray's SPLITHTTP transport),
- a non-`none` VLESS encryption key,
- `mldsa65Verify`,
- FinalMask Sudoku.

Everything else stays on sing-box. In both cases sing-box owns the VPN
interface, routing, DNS and socket protection; Xray receives a loopback SOCKS
listener and reaches the server through sing-box's existing mapping inbound, so
the server address is never exposed to an unprotected socket.

The Xray path rejects combinations Xray 26.10 cannot build: `allowInsecure`,
ECH, and the removed `http` (h2) / `quic` transports. Multiplexing is passed to
Xray as its own `mux` block; packet encoding is a sing-box knob and is simply
left unexpressed (Xray always relays UDP as XUDP). REALITY over the Xray path is
limited to `tcp`, `xhttp` and `grpc`, matching Xray itself, and XTLS Vision
cannot be combined with multiplexing.

## Links

`vless://` links are read and written with the v2rayNG parameter names:
`type` (`raw` is normalised to `tcp`, `splithttp` to `xhttp`), `headerType`,
`mode`, `extra`, `security`, `sni`, `alpn`, `fp`, `pbk`, `sid`, `spx`,
`mldsa65Verify`, `flow`, `encryption`, `packetEncoding` and `fm`.

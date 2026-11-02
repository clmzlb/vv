package io.nekohasekai.sagernet.fmt.v2ray

import com.google.gson.Gson
import com.google.gson.JsonObject
import com.google.gson.JsonParser

/**
 * The bundled Xray takes over the nodes sing-box cannot express: XHTTP, VLESS
 * Encryption and the ML-DSA-65 REALITY check. sing-box keeps VPN protection,
 * routing and DNS; Xray only owns the upstream connection.
 */
fun StandardV2RayBean.usesXrayBackend(): Boolean {
    if (!isVLESS) return false
    return usesFinalMask() ||
        type == "xhttp" ||
        (!vlessEncryption.isNullOrBlank() && vlessEncryption != "none") ||
        !mldsa65Verify.isNullOrBlank()
}

/**
 * Profiles stored before REALITY became its own option carry a TLS security
 * with a public key; keep reading them as REALITY.
 */
private fun StandardV2RayBean.effectiveSecurity(): String = when {
    security == "reality" -> "reality"
    security == "tls" && !realityPubKey.isNullOrBlank() -> "reality"
    else -> security.orEmpty()
}

fun StandardV2RayBean.validateVlessOptions() {
    validateFinalMask()
    require(type != "xhttp" || isVLESS) { "XHTTP requires a VLESS profile" }
    if (effectiveSecurity() == "reality") {
        require(!realityPubKey.isNullOrBlank()) { "REALITY requires a public key" }
    }
    if (!usesXrayBackend()) return

    require(isVLESS) { "The bundled Xray only runs VLESS profiles" }
    require(!enableECH) { "Xray backend: turn off ECH" }
    require(!enableMux) { "Xray backend: turn off multiplexing" }
    require(packetEncoding == 0) { "Xray backend: use the default packet encoding" }
    require(!allowInsecure) { "Xray 26 removed \"allow insecure\"; use a valid certificate" }
    require(type in XRAY_TRANSPORTS) { "Xray backend does not support transport: $type" }

    if (effectiveSecurity() == "reality") {
        require(type in setOf("tcp", "xhttp", "grpc")) {
            "Xray REALITY only supports TCP, XHTTP and gRPC"
        }
    }
    require(encryption != "xtls-rprx-vision" || effectiveSecurity() != "none") {
        "XTLS Vision requires TLS or REALITY"
    }
    if (type == "xhttp" && !xhttpExtra.isNullOrBlank()) {
        require(xhttpExtra.toJsonObjectOrNull() != null) { "XHTTP extra must be a JSON object" }
    }
}

private val XRAY_TRANSPORTS = setOf("tcp", "ws", "httpupgrade", "grpc", "xhttp")

private fun String.toJsonObjectOrNull(): JsonObject? = try {
    JsonParser.parseString(this).takeIf { it.isJsonObject }?.asJsonObject
} catch (_: RuntimeException) {
    null
}

/** Xray only handles this node. sing-box owns VPN protection, routing and DNS. */
fun StandardV2RayBean.buildXrayVlessConfig(port: Int): String {
    validateVlessOptions()
    require(usesXrayBackend()) { "This profile does not need the Xray backend" }

    val user = linkedMapOf<String, Any>(
        "id" to uuid.orEmpty(),
        "encryption" to vlessEncryption.orEmpty().ifBlank { "none" }
    )
    val flow = encryption.orEmpty()
    if (flow.isNotBlank() && flow != "none" && flow != "auto") {
        user["flow"] = flow
    }

    val securityMode = effectiveSecurity()

    val stream = linkedMapOf<String, Any>(
        "network" to type,
        "security" to securityMode
    )
    val streamPath = path.orEmpty()
    val streamHost = host.orEmpty()
    when (type) {
        "tcp" -> if (headerType == "http") {
            stream["tcpSettings"] = mapOf(
                "header" to mapOf(
                    "type" to "http",
                    "request" to linkedMapOf<String, Any>(
                        "path" to listOf(streamPath.ifBlank { "/" })
                    ).apply {
                        if (streamHost.isNotBlank()) {
                            put("headers", mapOf("Host" to listOf(streamHost)))
                        }
                    }
                )
            )
        }

        "ws" -> stream["wsSettings"] = linkedMapOf<String, Any>(
            "path" to streamPath.ifBlank { "/" }
        ).apply {
            if (streamHost.isNotBlank()) put("headers", mapOf("Host" to streamHost))
            if ((wsMaxEarlyData ?: 0) > 0) {
                put("maxEarlyData", wsMaxEarlyData ?: 0)
                if (!earlyDataHeaderName.isNullOrBlank()) {
                    put("earlyDataHeaderName", earlyDataHeaderName)
                }
            }
        }

        "httpupgrade" -> stream["httpupgradeSettings"] = linkedMapOf<String, Any>(
            "path" to streamPath.ifBlank { "/" }
        ).apply {
            if (streamHost.isNotBlank()) put("host", streamHost)
        }

        "grpc" -> stream["grpcSettings"] = mapOf("serviceName" to streamPath)

        "xhttp" -> stream["xhttpSettings"] = linkedMapOf<String, Any>(
            "path" to streamPath.ifBlank { "/" }
        ).apply {
            if (streamHost.isNotBlank()) put("host", streamHost)
            if (!xhttpMode.isNullOrBlank()) put("mode", xhttpMode)
            if (!xhttpExtra.isNullOrBlank()) {
                put("extra", JsonParser.parseString(xhttpExtra))
            }
        }
    }

    if (securityMode == "tls") {
        stream["tlsSettings"] = linkedMapOf<String, Any>(
            "serverName" to sni.orEmpty().ifBlank { serverAddress }
        ).apply {
            val alpnValue = alpn.orEmpty()
            if (alpnValue.isNotBlank()) {
                put("alpn", alpnValue.split(',', '\n').map { it.trim() }.filter { it.isNotEmpty() })
            }
            if (!utlsFingerprint.isNullOrBlank()) put("fingerprint", utlsFingerprint)
            val certs = certificates.orEmpty()
            if (certs.isNotBlank()) {
                put("certificates", listOf(mapOf(
                    "usage" to "verify", "certificate" to certs.lines()
                )))
            }
        }
    } else if (securityMode == "reality") {
        stream["realitySettings"] = linkedMapOf<String, Any>(
            "serverName" to sni.orEmpty().ifBlank { serverAddress },
            "publicKey" to realityPubKey.orEmpty(),
            "shortId" to realityShortId.orEmpty(),
            "fingerprint" to utlsFingerprint.orEmpty().ifBlank { "chrome" }
        ).apply {
            if (!spiderX.isNullOrBlank()) put("spiderX", spiderX)
            if (!mldsa65Verify.isNullOrBlank()) put("mldsa65Verify", mldsa65Verify)
        }
    }

    if (usesFinalMask()) {
        stream["finalmask"] = parseTcpSudokuFinalMask(finalMask)
    }

    return Gson().toJson(linkedMapOf(
        "log" to mapOf("loglevel" to "warning"),
        "inbounds" to listOf(mapOf(
            "tag" to "local", "listen" to "127.0.0.1", "port" to port, "protocol" to "socks",
            "settings" to mapOf("auth" to "noauth", "udp" to true, "ip" to "127.0.0.1")
        )),
        "outbounds" to listOf(mapOf(
            "tag" to "proxy", "protocol" to "vless",
            "settings" to mapOf("vnext" to listOf(mapOf(
                "address" to finalAddress, "port" to finalPort, "users" to listOf(user)
            ))), "streamSettings" to stream
        ))
    ))
}

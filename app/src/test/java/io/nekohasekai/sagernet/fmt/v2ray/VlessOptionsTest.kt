package io.nekohasekai.sagernet.fmt.v2ray

import com.google.gson.JsonParser
import io.nekohasekai.sagernet.database.ProxyEntity
import io.nekohasekai.sagernet.fmt.KryoConverters
import io.nekohasekai.sagernet.ktx.applyDefaultValues
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.URLEncoder

/** Mirrors the fields v2rayNG exposes for VLESS. */
class VlessOptionsTest {

    private val uuid = "00000000-0000-4000-8000-000000000001"
    private val host = "203.0.113.5"

    private fun parse(query: String) = parseV2Ray("vless://$uuid@$host:443?$query")
        .applyDefaultValues() as VMessBean

    private fun external(bean: VMessBean) = ProxyEntity().apply { putBean(bean) }.needExternal()

    private fun stream(bean: VMessBean) = JsonParser.parseString(bean.buildXrayVlessConfig(10000))
        .asJsonObject["outbounds"].asJsonArray.single().asJsonObject["streamSettings"].asJsonObject

    private fun prepare(bean: VMessBean) = bean.apply {
        finalAddress = "127.0.0.1"
        finalPort = 23456
    }

    @Test
    fun xhttpKeepsModeExtraAndRunsOnXray() {
        val extra = """{"xPaddingBytes":"100-1000","scMaxEachPostBytes":"1000000"}"""
        val bean = parse(
            "type=xhttp&security=tls&sni=example.com&host=cdn.example&path=%2Fapi" +
                "&mode=stream-one&extra=" + URLEncoder.encode(extra, "UTF-8")
        )
        assertEquals("xhttp", bean.type)
        assertEquals("stream-one", bean.xhttpMode)
        assertEquals(extra, bean.xhttpExtra)
        assertTrue(external(bean))

        val settings = stream(prepare(bean))
        assertEquals("xhttp", settings["network"].asString)
        val xhttp = settings["xhttpSettings"].asJsonObject
        assertEquals("/api", xhttp["path"].asString)
        assertEquals("cdn.example", xhttp["host"].asString)
        assertEquals("stream-one", xhttp["mode"].asString)
        assertEquals(
            "100-1000",
            xhttp["extra"].asJsonObject["xPaddingBytes"].asString
        )
        assertEquals("example.com", settings["tlsSettings"].asJsonObject["serverName"].asString)
    }

    @Test
    fun vlessEncryptionSwitchesToXray() {
        val key = "mlkem768x25519plus.native.600s.AAAA"
        val bean = parse("type=tcp&security=reality&sni=example.com&pbk=pub&sid=00&encryption=$key")
        assertEquals(key, bean.vlessEncryption)
        assertTrue(external(bean))

        val outbound = JsonParser.parseString(prepare(bean).buildXrayVlessConfig(10000))
            .asJsonObject["outbounds"].asJsonArray.single().asJsonObject
        val user = outbound["settings"].asJsonObject["vnext"].asJsonArray.single()
            .asJsonObject["users"].asJsonArray.single().asJsonObject
        assertEquals(key, user["encryption"].asString)
    }

    @Test
    fun realitySpiderAndMldsa65RunOnXray() {
        val bean = parse(
            "type=tcp&security=reality&sni=example.com&pbk=pub&sid=00&spx=%2Fprobe" +
                "&mldsa65Verify=deadbeef&flow=xtls-rprx-vision"
        )
        assertEquals("reality", bean.security)
        assertEquals("/probe", bean.spiderX)
        assertEquals("deadbeef", bean.mldsa65Verify)
        assertTrue(external(bean))

        val settings = stream(prepare(bean))
        assertEquals("reality", settings["security"].asString)
        val reality = settings["realitySettings"].asJsonObject
        assertEquals("pub", reality["publicKey"].asString)
        assertEquals("/probe", reality["spiderX"].asString)
        assertEquals("deadbeef", reality["mldsa65Verify"].asString)
        assertEquals("chrome", reality["fingerprint"].asString)
    }

    @Test
    fun plainRealityAlsoRunsOnXray() {
        val bean = parse("type=tcp&security=reality&sni=example.com&pbk=pub&sid=00&flow=xtls-rprx-vision")
        assertEquals("reality", bean.security)
        assertTrue(external(bean))
        assertTrue(bean.usesXrayBackend())
        val settings = stream(prepare(bean))
        assertEquals("tcp", settings["network"].asString)
        assertEquals("reality", settings["security"].asString)
        assertEquals("pub", settings["realitySettings"].asJsonObject["publicKey"].asString)
    }

    @Test
    fun tcpHeaderObfsBecomesXrayTcpSettings() {
        val bean = parse("type=tcp&headerType=http&security=none&host=edge.example&path=%2Fx")
        assertEquals("tcp", bean.type)
        assertEquals("http", bean.headerType)
        assertEquals("edge.example", bean.host)
        assertEquals("/x", bean.path)
        assertTrue(external(bean))

        val settings = stream(prepare(bean))
        assertEquals("tcp", settings["network"].asString)
        val request = settings["tcpSettings"].asJsonObject["header"].asJsonObject["request"].asJsonObject
        assertEquals("/x", request["path"].asJsonArray.single().asString)
        assertEquals("edge.example", request["headers"].asJsonObject["Host"].asJsonArray.single().asString)
    }

    @Test
    fun xhttpIsRejectedForNonVlessProfiles() {
        val trojan = io.nekohasekai.sagernet.fmt.trojan.TrojanBean().apply {
            serverAddress = host
            serverPort = 443
            password = "secret"
            type = "xhttp"
            security = "tls"
        }.applyDefaultValues()
        val error = assertThrows(IllegalArgumentException::class.java) {
            trojan.validateVlessOptions()
        }
        assertTrue(error.message!!.contains("XHTTP"))
    }

    @Test
    fun malformedExtraIsRejected() {
        val bean = parse("type=xhttp&security=tls&path=%2F&mode=auto&extra=not-json")
        assertThrows(IllegalArgumentException::class.java) { bean.validateVlessOptions() }
    }

    @Test
    fun shareLinkAndDatabaseKeepEveryField() {
        val bean = parse(
            "type=xhttp&security=reality&sni=example.com&pbk=pub&sid=00&spx=%2Fprobe" +
                "&host=cdn.example&path=%2Fapi&mode=packet-up&flow=xtls-rprx-vision"
        )
        for (copy in listOf(
            bean.clone(),
            KryoConverters.vmessDeserialize(KryoConverters.serialize(bean)),
            parseV2Ray(bean.toUriVMessVLESSTrojan(false)).applyDefaultValues()
        )) {
            assertEquals(bean, copy)
            assertEquals("xhttp", copy.type)
            assertEquals("packet-up", copy.xhttpMode)
            assertEquals("reality", copy.security)
            assertEquals("/probe", copy.spiderX)
            assertEquals("xtls-rprx-vision", copy.encryption)
        }
    }

    @Test
    fun exportedLinkCarriesV2rayNgParameterNames() {
        val bean = parse(
            "type=xhttp&security=reality&sni=example.com&pbk=pub&sid=00&spx=%2Fprobe" +
                "&host=cdn.example&path=%2Fapi&mode=stream-up&encryption=none"
        )
        val link = bean.toUriVMessVLESSTrojan(false)
        for (part in listOf(
            "type=xhttp", "security=reality", "pbk=pub", "sid=00",
            "mode=stream-up", "encryption=none", "host=cdn.example"
        )) {
            assertTrue("missing $part in $link", link.contains(part))
        }
        assertTrue(link.contains("spx=/probe") || link.contains("spx=%2Fprobe"))
        assertTrue(link.contains("path=/api") || link.contains("path=%2Fapi"))
    }

    @Test
    fun xrayBackendCarriesMuxAndIgnoresPacketEncoding() {
        val bean = parse("type=xhttp&security=tls&sni=example.com&path=%2F&mode=auto").apply {
            enableMux = true
            muxConcurrency = 8
            packetEncoding = 2
        }
        bean.validateVlessOptions()
        val outbound = JsonParser.parseString(prepare(bean).buildXrayVlessConfig(10000))
            .asJsonObject["outbounds"].asJsonArray.single().asJsonObject
        val mux = outbound["mux"].asJsonObject
        assertTrue(mux["enabled"].asBoolean)
        assertEquals(8, mux["concurrency"].asInt)
    }

    @Test
    fun visionCannotBeMultiplexed() {
        // mldsa65Verify forces the Xray backend, where Xray itself refuses mux + Vision.
        val bean = parse(
            "type=tcp&security=reality&sni=example.com&pbk=pub&sid=00&mldsa65Verify=deadbeef" +
                "&flow=xtls-rprx-vision"
        ).apply { enableMux = true }
        val error = assertThrows(IllegalArgumentException::class.java) {
            bean.validateVlessOptions()
        }
        assertTrue(error.message!!.contains("Vision"))
    }

    @Test
    fun ordinaryVlessKeepsItsDefaultsAndStillUsesXray() {
        val bean = parse("type=ws&security=tls&sni=example.com&path=%2Fws")
        assertEquals("", bean.vlessEncryption)
        assertEquals("none", bean.headerType)
        assertEquals("", bean.xhttpMode)
        assertEquals("", bean.spiderX)
        assertNotNull(bean.serverAddress)
        assertTrue(bean.usesXrayBackend())
        val settings = stream(prepare(bean))
        assertEquals("ws", settings["network"].asString)
        assertEquals("/ws", settings["wsSettings"].asJsonObject["path"].asString)
    }
}

package io.nekohasekai.sagernet.fmt.v2ray

import com.google.gson.JsonObject
import com.google.gson.JsonParser

/** Keep the wire JSON intact, including optional Sudoku tables and padding. */
fun parseTcpSudokuFinalMask(text: String): JsonObject {
    require(text.toByteArray(Charsets.UTF_8).size <= 16384) { "FinalMask configuration is too large" }
    try {
        val root = JsonParser.parseString(text).asJsonObject
        require(root.keySet() == setOf("tcp")) { "Only FinalMask Sudoku over TCP is supported" }
        val masks = root["tcp"].asJsonArray
        require(masks.size() == 1) { "Use one Sudoku TCP mask" }
        val mask = masks.single().asJsonObject
        require(mask.keySet() == setOf("type", "settings") && mask["type"].asString == "sudoku") {
            "Only the Sudoku TCP mask is supported"
        }
        val settings = mask["settings"].asJsonObject
        require(settings.keySet().all { it in setOf("password", "ascii", "customTable", "customTables", "paddingMin", "paddingMax") }) {
            "Unsupported Sudoku setting"
        }
        fun string(key: String): String? = settings[key]?.let {
            require(it.isJsonPrimitive && it.asJsonPrimitive.isString) { "Sudoku $key must be a string" }
            it.asString
        }
        require(!string("password").isNullOrEmpty()) { "Sudoku password is required" }
        require(string("ascii") in listOf(null, "", "prefer_ascii", "prefer_entropy")) { "Invalid Sudoku ASCII mode" }
        string("customTable")
        settings["customTables"]?.let { tables ->
            require(tables.isJsonArray && tables.asJsonArray.all { it.isJsonPrimitive && it.asJsonPrimitive.isString }) {
                "Sudoku customTables must be an array of strings"
            }
        }
        fun padding(key: String): Int = settings[key]?.let {
            require(it.isJsonPrimitive && it.asJsonPrimitive.isNumber && it.toString().matches(Regex("[0-9]+"))) {
                "Sudoku $key must be an integer"
            }
            val value = it.asString.toIntOrNull()
            require(value != null && value in 0..100) { "Sudoku padding must be between 0 and 100" }
            value
        } ?: 0
        require(padding("paddingMin") <= padding("paddingMax")) { "Sudoku paddingMin exceeds paddingMax" }
        return root
    } catch (error: IllegalArgumentException) {
        throw error
    } catch (error: RuntimeException) {
        throw IllegalArgumentException("Invalid FinalMask Sudoku JSON", error)
    }
}

fun StandardV2RayBean.usesFinalMask(): Boolean = !finalMask.isNullOrBlank()

fun StandardV2RayBean.validateFinalMask() {
    if (!usesFinalMask()) return
    require(isVLESS && type == "tcp") { "FinalMask Sudoku requires VLESS over TCP" }
    require(security in listOf("none", "tls", "reality")) { "Unsupported Sudoku transport security" }
    require(!enableMux) { "FinalMask Sudoku requires multiplexing off" }
    require(encryption in listOf("", "none", "xtls-rprx-vision")) { "Unsupported VLESS flow" }
    require(encryption != "xtls-rprx-vision" || security != "none") { "XTLS Vision requires TLS or REALITY" }
    parseTcpSudokuFinalMask(finalMask)
}

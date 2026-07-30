/*
 * packer_obfuscation.yar
 * 检测加壳/混淆/加密规避
 */

rule UPX_Packed
{
    meta:
        description = "UPX 加壳检测"
        severity    = "medium"
        category    = "packer"

    strings:
        $upx1 = "UPX!" ascii
        $upx2 = "UPX0" ascii
        $upx3 = "UPX1" ascii
        $upx4 = "UPX2" ascii
        $upx_sig = { 55 50 58 21 }  // "UPX!"

    condition:
        $upx_sig or all of ($upx1, $upx2, $upx3, $upx4)
}

rule TheMPress_Packed
{
    meta:
        description = "Themida/WinLicense 加壳"
        severity    = "high"
        category    = "packer"

    strings:
        $themida1 = "Themida" ascii wide
        $themida2 = "WinLicense" ascii wide
        $themida3 = { 54 68 65 6D 69 64 61 }  // "Themida"

    condition:
        1 of them
}

rule VMProtect_Packed
{
    meta:
        description = "VMProtect 虚拟化保护"
        severity    = "high"
        category    = "packer"

    strings:
        $vmp1 = "VMProtect" ascii wide
        $vmp2 = "vmp0" ascii
        $vmp3 = "vmp1" ascii
        $vmp4 = { 56 4D 50 72 6F 74 65 63 74 }  // "VMProtect"

    condition:
        1 of them
}

rule ASPack_Packed
{
    meta:
        description = "ASPack 加壳"
        severity    = "medium"
        category    = "packer"

    strings:
        $asp1 = "ASPack" ascii
        $asp2 = ".aspack" ascii
        $asp3 = { 61 73 70 61 63 6B }  // "aspack"

    condition:
        1 of them
}

rule ConfuserEx_Obfuscated
{
    meta:
        description = ".NET ConfuserEx 混淆"
        severity    = "medium"
        category    = "obfuscation"

    strings:
        $conf1 = "ConfusedBy" ascii
        $conf2 = "ConfuserEx" ascii
        $conf3 = /\b[A-Za-z]{30,}\.[A-Za-z]{30,}\./  // 混淆命名空间

    condition:
        1 of them
}

rule HighEntropy_EncryptedPayload
{
    meta:
        description = "高熵区域 (可能为加密/压缩规避)"
        severity    = "low"
        category    = "obfuscation"

    strings:
        // 连续不可打印字符 > 256 字节
        $high_entropy = /[\x00-\x08\x0E-\x1F\x7F-\xFF]{256,}/

    condition:
        $high_entropy
}

rule Base64_ObfuscatedSource
{
    meta:
        description = "Base64 编码的源码 (规避检测)"
        severity    = "medium"
        category    = "obfuscation"

    strings:
        $b64_py  = /[A-Za-z0-9+\/]{40,}={0,2}\s*\n[A-Za-z0-9+\/]{40,}={0,2}/  // 多行base64
        $b64_1   = /[A-Za-z0-9+\/]{100,}={0,2}/

    condition:
        $b64_py or ($b64_1 and filesize < 1MB)
}

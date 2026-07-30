/*
 * embedded_archive.yar
 * 检测二进制/文件中嵌入的压缩包或文件系统镜像
 */

rule Embedded_ZIP
{
    meta:
        description = "检测嵌入的 ZIP 归档 (PK signature)"
        severity    = "medium"
        category    = "embedded_archive"

    strings:
        $zip_sig  = { 50 4B 03 04 }       // PK\x03\x04 - local file header
        $zip_eocd = { 50 4B 05 06 }       // PK\x05\x06 - end of central directory
        $zip_64   = { 50 4B 06 07 }       // PK\x06\x07 - ZIP64 EOCD

    condition:
        $zip_sig at 0 or ($zip_sig and $zip_eocd) or $zip_64
}

rule Embedded_GZIP
{
    meta:
        description = "检测嵌入的 GZIP 流"
        severity    = "low"
        category    = "embedded_archive"

    strings:
        $gzip = { 1F 8B 08 }  // gzip magic + deflate

    condition:
        $gzip at 0
}

rule Embedded_BZIP2
{
    meta:
        description = "检测嵌入的 BZIP2 流"
        severity    = "low"
        category    = "embedded_archive"

    strings:
        $bz = "BZh" nocase

    condition:
        $bz at 0
}

rule Embedded_XZ
{
    meta:
        description = "检测嵌入的 XZ/LZMA 流"
        severity    = "low"
        category    = "embedded_archive"

    strings:
        $xz = { FD 37 7A 58 5A 00 }  // xz magic

    condition:
        $xz at 0
}

rule Embedded_7Z
{
    meta:
        description = "检测嵌入的 7-Zip 归档"
        severity    = "medium"
        category    = "embedded_archive"

    strings:
        $7z = { 37 7A BC AF 27 1C }  // 7z signature

    condition:
        $7z at 0
}

rule Embedded_RAR
{
    meta:
        description = "检测嵌入的 RAR 归档"
        severity    = "medium"
        category    = "embedded_archive"

    strings:
        $rar  = "Rar!\x1a\x07" nocase
        $rar5 = "Rar!\x1a\x07\x01\x00" nocase

    condition:
        $rar or $rar5
}

rule Embedded_TAR
{
    meta:
        description = "检测 TAR 归档 (ustar/POSIX)"
        severity    = "low"
        category    = "embedded_archive"

    strings:
        $ustar  = "ustar" nocase
        $tar_hdr = /\x00{8}(ustar|\x20{8})/ nocase

    condition:
        $ustar or $tar_hdr
}

rule Embedded_SquashFS
{
    meta:
        description = "检测嵌入的 SquashFS 文件系统"
        severity    = "medium"
        category    = "embedded_archive"

    strings:
        $sqsh1 = "sqsh" ascii
        $sqsh2 = "hsqs" ascii
        $sqsh3 = "shsq" ascii
        $sqsh4 = "qshs" ascii

    condition:
        1 of them
}

rule Embedded_CramFS
{
    meta:
        description = "检测嵌入的 CramFS 文件系统"
        severity    = "medium"
        category    = "embedded_archive"

    strings:
        $cram1 = "Compressed ROMFS" ascii
        $cram2 = { 45 3D CD 28 }  // cramfs superblock magic

    condition:
        1 of them
}

rule Embedded_ISO
{
    meta:
        description = "检测嵌入的 ISO 9660 镜像"
        severity    = "low"
        category    = "embedded_archive"

    strings:
        $iso = "CD001" ascii

    condition:
        $iso
}

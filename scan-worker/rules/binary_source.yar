/*
 * binary_source.yar
 * 检测 ELF/PE/Mach-O 等二进制文件中夹带的源码字符串
 */

rule Binary_Embedded_C_Code
{
    meta:
        description = "ELF/PE 中夹带的 C/C++ 源码字符串"
        severity    = "high"
        category    = "binary_source_leak"

    strings:
        $c1 = /\/\*.*Copyright.*\*\// nocase
        $c2 = /\/\/\s*(Author|TODO|FIXME|HACK)/ nocase
        $c3 = /\bint\s+main\s*\(\s*(void|int)/ nocase
        $c4 = /\b#include\s*<(stdio|stdlib|string|pthread|unistd)\.h>/ nocase
        $c5 = /\b(struct\s+\w+\s*\{[^}]*\})/ nocase
        $c6 = /\breturn\s+\d+;\s*\n\s*\}/ nocase

    condition:
        all of ($c1, $c2, $c3) or 4 of them
}

rule Binary_Embedded_Python
{
    meta:
        description = "二进制中夹带的 Python 源码"
        severity    = "high"
        category    = "binary_source_leak"

    strings:
        $py1 = /\bdef\s+\w+\s*\([^)]*\)\s*:/ nocase
        $py2 = /\bimport\s+(os|sys|subprocess|socket|ctypes|json|re)/ nocase
        $py3 = /\bfrom\s+\w+\s+import\s+\*/ nocase
        $py4 = /\bif\s+__name__\s*==\s*["']__main__["']/ nocase
        $py5 = /\bprint\s*\(\s*["'][^"']{4,}["']\s*\)/ nocase
        $py6 = /^\s*@\w+\s*$/ nocase  // python decorator

    condition:
        3 of them
}

rule Binary_Embedded_Go
{
    meta:
        description = "二进制中夹带的 Go 源码特征"
        severity    = "high"
        category    = "binary_source_leak"

    strings:
        $go1 = /\bpackage\s+main\s*\n/ nocase
        $go2 = /\bfunc\s+main\s*\(\)/ nocase
        $go3 = /\bimport\s+\(\s*"[\w\/]+"/ nocase
        $go4 = /\bif\s+err\s*!=\s*nil\s*\{/ nocase
        $go5 = /\bfmt\.(Print|Println|Printf)\(/ nocase

    condition:
        3 of them
}

rule Binary_Embedded_SourceStrings
{
    meta:
        description = "二进制 .rodata/.data 段中的源码特征字符串"
        severity    = "medium"
        category    = "binary_source_leak"

    strings:
        $src1 = ".c" fullword ascii
        $src2 = ".cpp" fullword ascii
        $src3 = ".py" fullword ascii
        $src4 = ".go" fullword ascii
        $src5 = ".rs" fullword ascii
        $src6 = "main.c" ascii
        $src7 = "Makefile" ascii
        $src8 = "CMakeLists.txt" ascii
        $src9 = "/src/" ascii
        $src10= "/include/" ascii

    condition:
        4 of them
}

rule Binary_HighEntropy_CompressedPayload
{
    meta:
        description = "二进制中高熵区域 (可能内嵌加密/压缩源码)"
        severity    = "low"
        category    = "binary_source_leak"

    strings:
        $entropy_hint1 = /\x00{8,}/  // padding then data
        $entropy_hint2 = /\xDE\xAD\xBE\xEF/  // common marker
        $entropy_hint3 = /\xCA\xFE\xBA\xBE/  // Java class marker

    condition:
        all of them
}

rule ELF_With_DebugInfo
{
    meta:
        description = "ELF 包含调试信息 (可能泄露源码路径)"
        severity    = "low"
        category    = "binary_source_leak"

    strings:
        $strtab = ".strtab" ascii
        $debug   = ".debug_" ascii
        $dwarf   = "DWARF" ascii
        $srcpath = /\/home\/\w+\/[\w\/]+\.(c|h|cpp|go|rs|py)/ nocase

    condition:
        ($strtab and $debug) or $dwarf or $srcpath
}

rule PE_With_Resources
{
    meta:
        description = "PE 文件资源段可能包含源码"
        severity    = "medium"
        category    = "binary_source_leak"

    strings:
        $rsrc  = ".rsrc" ascii
        $src_in_rsrc = /\.text\.asm/ nocase
        $script = /\.vbs|\.ps1|\.bat/ nocase

    condition:
        $rsrc and ($src_in_rsrc or $script)
}

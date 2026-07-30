/*
 * source_code_leak.yar
 * 检测压缩包/二进制中暗藏的各类源代码
 */

rule C_SourceCode
{
    meta:
        description = "C/C++ 源码指纹"
        severity    = "high"
        category    = "source_code_leak"

    strings:
        $include1 = /\s*#\s*include\s*<\w+\.\w+>/ nocase
        $include2 = /\s*#\s*include\s*"\w+\.\w+"/ nocase
        $main     = /\bint\s+main\s*\(\s*(int|void)/ nocase
        $func     = /\b(void|int|char|double|float|long|short|unsigned|struct\s+\w+)\s+\w+\s*\([^;]*\)\s*\{/ nocase
        $typedef  = /\btypedef\s+(struct|enum|union|void|int|char)\b/ nocase
        $pragma   = /\s*#\s*pragma\s+(once|pack|comment)/ nocase

    condition:
        3 of them
}

rule Cpp_SourceCode
{
    meta:
        description = "C++ 源码指纹 (class, template, namespace)"
        severity    = "high"
        category    = "source_code_leak"

    strings:
        $class    = /\bclass\s+\w+\s*[:\{]/ nocase
        $template = /\btemplate\s*<\s*typename\s+\w+/ nocase
        $namespace= /\bnamespace\s+\w+\s*\{/ nocase
        $std      = /\bstd::\w+/ nocase
        $virtual  = /\bvirtual\s+\w+\s+\w+\s*\(/ nocase
        $override = /\b\w+\s+override\s*[\{;]/ nocase

    condition:
        3 of them
}

rule Go_SourceCode
{
    meta:
        description = "Go 语言源码指纹"
        severity    = "high"
        category    = "source_code_leak"

    strings:
        $package = /\bpackage\s+\w+\s*\n/ nocase
        $import  = /\bimport\s*\(/ nocase
        $import2 = /\bimport\s+"[\w\/]+"/ nocase
        $func    = /\bfunc\s+\(?(\w+\s+)?\w+\s*\([^)]*\)\s*[\(\{]/ nocase
        $goerr   = /\bif\s+err\s*!=\s*nil\s*\{/ nocase
        $defer   = /\bdefer\s+\w+/ nocase
        $goroutine = /\bgo\s+func\s*\(/ nocase

    condition:
        3 of them
}

rule Rust_SourceCode
{
    meta:
        description = "Rust 语言源码指纹"
        severity    = "high"
        category    = "source_code_leak"

    strings:
        $fn      = /\bfn\s+\w+\s*\([^)]*\)(\s*->\s*\w+)?\s*[\{;]/ nocase
        $let     = /\blet\s+(mut\s+)?\w+\s*[:=]/ nocase
        $use     = /\buse\s+\w+(\s*::\s*\w+)+/ nocase
        $impl    = /\bimpl\s+(\w+\s+)*\w+\s+for\s+\w+/ nocase
        $trait   = /\btrait\s+\w+/ nocase
        $match   = /\bmatch\s+\w+\s*\{/ nocase
        $cargo   = /\bprintln!\s*\(/ nocase

    condition:
        3 of them
}

rule Python_SourceCode
{
    meta:
        description = "Python 源码指纹"
        severity    = "high"
        category    = "source_code_leak"

    strings:
        $def      = /^\s*def\s+\w+\s*\([^)]*\)\s*:/ nocase
        $class    = /^\s*class\s+\w+\s*[\(:]/ nocase
        $import   = /^\s*import\s+\w+/ nocase
        $fromimp  = /^\s*from\s+\w+\s+import\s+/ nocase
        $self     = /\bself\.\w+\s*=/ nocase
        $async    = /^\s*async\s+def\s+/ nocase
        $try      = /^\s*try\s*:/ nocase
        $with     = /^\s*with\s+open\s*\(/ nocase

    condition:
        3 of them
}

rule Java_SourceCode
{
    meta:
        description = "Java 源码指纹"
        severity    = "high"
        category    = "source_code_leak"

    strings:
        $class    = /\bpublic\s+class\s+\w+\s*[\{]/ nocase
        $import   = /\bimport\s+[\w\.]+\s*;/ nocase
        $package  = /\bpackage\s+[\w\.]+\s*;/ nocase
        $main     = /\bpublic\s+static\s+void\s+main\s*\(/ nocase
        $extends  = /\bextends\s+\w+/ nocase
        $implements = /\bimplements\s+\w+/ nocase
        $system   = /\bSystem\.(out|err)\.print/ nocase

    condition:
        3 of them
}

rule JavaScript_SourceCode
{
    meta:
        description = "JavaScript/TypeScript 源码指纹"
        severity    = "medium"
        category    = "source_code_leak"

    strings:
        $const    = /\b(const|let|var)\s+\w+\s*=/ nocase
        $func     = /\bfunction\s+\w+\s*\(/ nocase
        $arrow    = /\(\s*\w+\s*\)\s*=>/ nocase
        $import   = /\bimport\s+\{[^}]*\}\s+from\s+/ nocase
        $export   = /\bexport\s+(default\s+)?(class|function|const)/ nocase
        $require  = /\brequire\s*\(\s*["'][\w\.\/]+["']\s*\)/ nocase
        $async    = /\basync\s+(\(\s*\)|function)\s*[\(\{]/ nocase

    condition:
        3 of them
}

rule Shell_SourceCode
{
    meta:
        description = "Shell 脚本源码"
        severity    = "medium"
        category    = "source_code_leak"

    strings:
        $shebang1 = /^#!\/bin\/(ba)?sh/ nocase
        $shebang2 = /^#!\/usr\/bin\/env\s+(ba)?sh/ nocase
        $if       = /\bif\s+\[\s+/ nocase
        $for      = /\bfor\s+\w+\s+in\s+/ nocase
        $pipe     = /\|\s*(grep|awk|sed|sort|uniq|cut|wc)\b/ nocase
        $export   = /\bexport\s+\w+=/ nocase

    condition:
        2 of them
}

rule SourceCode_FileExtension
{
    meta:
        description = "通过文件名匹配源码文件扩展名 (用于归档内文件)"
        severity    = "medium"
        category    = "source_code_leak"

    strings:
        $c_ext    = /\.(c|h|cpp|cc|cxx|hpp|hxx)$/ nocase
        $go_ext   = /\.go$/ nocase
        $rs_ext   = /\.rs$/ nocase
        $py_ext   = /\.py$/ nocase
        $java_ext = /\.(java|kt|scala)$/ nocase
        $js_ext   = /\.(js|jsx|ts|tsx)$/ nocase
        $rb_ext   = /\.rb$/ nocase
        $php_ext  = /\.php$/ nocase
        $cs_ext   = /\.cs$/ nocase
        $sh_ext   = /\.(sh|bash|zsh)$/ nocase
        $swift_ext= /\.swift$/ nocase
        $sql_ext  = /\.sql$/ nocase

    condition:
        1 of them
}

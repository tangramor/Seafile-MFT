/*
 * secret_token.yar
 * 检测源码/二进制中泄露的密钥、令牌、私钥
 */

rule AWS_AccessKey
{
    meta:
        description = "AWS Access Key ID"
        severity    = "critical"
        category    = "secret_leak"

    strings:
        $aws = /\b(AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA)[A-Z0-9]{16}\b/

    condition:
        $aws
}

rule AWS_SecretKey
{
    meta:
        description = "AWS Secret Access Key (high entropy base64)"
        severity    = "critical"
        category    = "secret_leak"

    strings:
        $aws_sec = /\baws_secret_access_key\s*=\s*["']?[A-Za-z0-9\/+=]{40}["']?/ nocase

    condition:
        $aws_sec
}

rule GitHub_Token
{
    meta:
        description = "GitHub Personal Access Token / OAuth"
        severity    = "critical"
        category    = "secret_leak"

    strings:
        $gh1 = /\bghp_[A-Za-z0-9]{36}\b/
        $gh2 = /\bgho_[A-Za-z0-9]{36}\b/
        $gh3 = /\bghs_[A-Za-z0-9]{36}\b/
        $gh4 = /\bghr_[A-Za-z0-9]{36}\b/

    condition:
        1 of them
}

rule Google_API_Key
{
    meta:
        description = "Google API Key"
        severity    = "critical"
        category    = "secret_leak"

    strings:
        $gkey = /\bAIza[0-9A-Za-z_\-]{35}\b/

    condition:
        $gkey
}

rule Slack_Token
{
    meta:
        description = "Slack Token"
        severity    = "critical"
        category    = "secret_leak"

    strings:
        $slack1 = /\bxox[baprs]-[A-Za-z0-9-]{10,80}\b/
        $slack2 = /\bslack_secret\s*=\s*["']?[A-Za-z0-9]{32}["']?/ nocase

    condition:
        1 of them
}

rule PrivateKey_PEM
{
    meta:
        description = "PEM 格式私钥 (RSA/EC/OPENSSH)"
        severity    = "critical"
        category    = "secret_leak"

    strings:
        $rsa  = "-----BEGIN RSA PRIVATE KEY-----" ascii
        $ec   = "-----BEGIN EC PRIVATE KEY-----" ascii
        $dsa  = "-----BEGIN DSA PRIVATE KEY-----" ascii
        $gen  = "-----BEGIN PRIVATE KEY-----" ascii
        $openssh = "-----BEGIN OPENSSH PRIVATE KEY-----" ascii
        $pvk  = "-----BEGIN PVK PRIVATE KEY-----" ascii

    condition:
        1 of them
}

rule JWT_Token
{
    meta:
        description = "JWT Token (header.payload.signature)"
        severity    = "high"
        category    = "secret_leak"

    strings:
        $jwt = /\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}/

    condition:
        $jwt
}

rule Generic_API_Key
{
    meta:
        description = "通用 API Key / Secret 变量赋值"
        severity    = "high"
        category    = "secret_leak"

    strings:
        $api1 = /\b(api_key|apikey|api-key)\s*[:=]\s*["'][A-Za-z0-9_\-]{20,}["']/ nocase
        $api2 = /\b(secret|passwd|password|pwd)\s*[:=]\s*["'][^"']{8,}["']/ nocase
        $api3 = /\b(access_token|refresh_token|client_secret)\s*[:=]\s*["'][A-Za-z0-9_\-]{20,}["']/ nocase

    condition:
        1 of them
}

rule Database_ConnectionString
{
    meta:
        description = "数据库连接字符串 (含密码)"
        severity    = "high"
        category    = "secret_leak"

    strings:
        $pg  = /\bpostgres(ql)?:\/\/\w+:\w+@\w+/ nocase
        $mysql = /\bmysql:\/\/\w+:\w+@\w+/ nocase
        $mongo = /\bmongodb(\+srv)?:\/\/\w+:\w+@\w+/ nocase
        $redis = /\bredis:\/\/:\w+@\w+/ nocase
        $ora  = /\b(ora|oracle):\/\/\w+:\w+@\w+/ nocase

    condition:
        1 of them
}

rule SSH_PrivateKeyBlock
{
    meta:
        description = "SSH 私钥内容特征"
        severity    = "critical"
        category    = "secret_leak"

    strings:
        $ssh1 = "ssh-rsa " ascii
        $ssh2 = "ssh-ed25519 " ascii
        $ssh3 = "ssh-dss " ascii
        $ssh4 = "PuTTY-User-Key-File-2" ascii

    condition:
        1 of them
}

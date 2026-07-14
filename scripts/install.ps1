[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$repository = "fiftidollar/okk-analytics-codex-plugin"
$marketplace = "alpes-community"
$plugin = "okk-analytics@$marketplace"
$configPath = Join-Path $HOME ".codex/config.toml"

if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    throw "Codex CLI is not available on PATH."
}

$config = if (Test-Path -LiteralPath $configPath) {
    [IO.File]::ReadAllText($configPath)
} else {
    ""
}

$compatibilityArgs = @()
if ($config -match '(?m)^service_tier\s*=\s*"default"\s*$') {
    $compatibilityArgs += @("-c", "service_tier=fast")
}
$defaultPermissions = [regex]::Match(
    $config,
    '(?m)^default_permissions\s*=\s*"([^"]+)"\s*$'
).Groups[1].Value
if ($defaultPermissions.StartsWith(":")) {
    $fallbackProfile = [regex]::Match(
        $config,
        '(?m)^\[permissions\."([^"]+)"\.filesystem\]\s*$'
    ).Groups[1].Value
    if ($fallbackProfile) {
        $compatibilityArgs += @("-c", "default_permissions=`"$fallbackProfile`"")
    }
}

if ($config -match "(?m)^\[marketplaces\.$([regex]::Escape($marketplace))\]$") {
    & codex @compatibilityArgs plugin marketplace upgrade $marketplace
} else {
    & codex @compatibilityArgs plugin marketplace add $repository
}
if ($LASTEXITCODE -ne 0) {
    throw "Could not add or update the $marketplace marketplace."
}

$pluginHelp = (& codex plugin --help 2>&1 | Out-String)
if ($pluginHelp -match "(?m)^\s+add\s") {
    & codex @compatibilityArgs plugin add $plugin
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install $plugin."
    }
} else {
    $section = "[plugins.`"$plugin`"]"
    if ($config -match "(?m)^$([regex]::Escape($section))$") {
        $sectionPattern = "(?ms)^$([regex]::Escape($section))\r?\n(?<body>.*?)(?=^\[|\z)"
        $config = [regex]::Replace(
            $config,
            $sectionPattern,
            {
                param($match)
                $body = $match.Groups["body"].Value
                if ($body -match "(?m)^enabled\s*=") {
                    $body = [regex]::Replace($body, "(?m)^enabled\s*=.*$", "enabled = true")
                } else {
                    $body = "enabled = true`r`n$body"
                }
                "$section`r`n$body"
            }
        )
    } else {
        $separator = if ($config.EndsWith("`n") -or -not $config) { "" } else { "`r`n" }
        $config += "$separator`r`n$section`r`nenabled = true`r`n"
    }
    $configDirectory = Split-Path -Parent $configPath
    [IO.Directory]::CreateDirectory($configDirectory) | Out-Null
    [IO.File]::WriteAllText($configPath, $config, [Text.UTF8Encoding]::new($false))
}

Write-Host "Installed $plugin. Start a new Codex task to load its skill and MCP tools."

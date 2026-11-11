param([Parameter(Mandatory=$true)][string]$MacAddress)

$ErrorActionPreference = 'Stop'
$wanted = $MacAddress -replace '[:-]', ''
function Find-TV {
    # arp.exe works without the CIM permissions required by Get-NetNeighbor.
    $arpMatches = @(arp.exe -a | ForEach-Object {
        if ($_ -match '^\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f-]{17})\s+' -and
            ($Matches[2] -replace '-', '') -ieq $wanted) { $Matches[1] }
    })
    if ($arpMatches.Count) { $arpMatches | Select-Object -Unique; return }
    Get-NetNeighbor -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {
        ($_.LinkLayerAddress -replace '[:-]', '') -ieq $wanted
    } | Select-Object -ExpandProperty IPAddress -Unique
}

$found = @(Find-TV)
if ($found.Count) { $found; exit 0 }

# Populate the neighbor table on small, directly attached LANs. Ping replies
# are not required: Windows resolves the MAC before sending the echo request.
$probes = @()
try {
    $addresses = Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
        $_.AddressState -eq 'Preferred' -and $_.PrefixLength -ge 24 -and
        $_.PrefixLength -le 30 -and $_.IPAddress -notlike '127.*' -and
        $_.IPAddress -notlike '169.254.*'
    }
    foreach ($address in $addresses) {
        $octets = $address.IPAddress.Split('.')
        $size = [int][Math]::Pow(2, 32 - $address.PrefixLength)
        $start = [int]([Math]::Floor([int]$octets[3] / $size) * $size)
        $prefix = $octets[0..2] -join '.'
        foreach ($hostNumber in ($start + 1)..($start + $size - 2)) {
            $target = "$prefix.$hostNumber"
            if ($target -eq $address.IPAddress) { continue }
            $ping = New-Object System.Net.NetworkInformation.Ping
            $probes += [PSCustomObject]@{ Ping=$ping; Task=$ping.SendPingAsync($target, 700) }
        }
    }
    if ($probes.Count) {
        try { [System.Threading.Tasks.Task]::WaitAll([System.Threading.Tasks.Task[]]$probes.Task, 2500) | Out-Null } catch { }
    }
    Find-TV
} finally {
    foreach ($probe in $probes) { $probe.Ping.Dispose() }
}

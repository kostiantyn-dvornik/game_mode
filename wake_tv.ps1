param([Parameter(Mandatory=$true)][string]$MacAddress)
$ErrorActionPreference = 'Stop'
$hex = $MacAddress -replace '[:-]', ''
if ($hex -notmatch '^[0-9a-fA-F]{12}$') { throw 'Invalid TV MAC address' }
$packet = New-Object byte[] 102
for ($i = 0; $i -lt 6; $i++) { $packet[$i] = 255 }
for ($i = 6; $i -lt 102; $i++) {
    $packet[$i] = [Convert]::ToByte($hex.Substring((($i - 6) % 6) * 2, 2), 16)
}
$sent = 0
foreach ($adapter in [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
    if ($adapter.OperationalStatus -ne 'Up') { continue }
    foreach ($address in $adapter.GetIPProperties().UnicastAddresses) {
        if ($address.Address.AddressFamily -ne 'InterNetwork' -or
            [Net.IPAddress]::IsLoopback($address.Address)) { continue }
        $ip = $address.Address.GetAddressBytes()
        $mask = $address.IPv4Mask.GetAddressBytes()
        $broadcast = New-Object byte[] 4
        for ($i = 0; $i -lt 4; $i++) { $broadcast[$i] = $ip[$i] -bor (255 -bxor $mask[$i]) }
        $target = New-Object Net.IPAddress -ArgumentList (,$broadcast)
        $udp = New-Object Net.Sockets.UdpClient
        try {
            $udp.EnableBroadcast = $true
            $udp.Client.Bind((New-Object Net.IPEndPoint($address.Address, 0)))
            foreach ($port in @(9, 7)) {
                $endpoint = New-Object Net.IPEndPoint($target, $port)
                [void]$udp.Send($packet, $packet.Length, $endpoint)
                $sent++
            }
            Write-Output "Wake packets sent via $($adapter.Name) to $target (ports 9 and 7)."
        } finally { $udp.Dispose() }
    }
}
if (-not $sent) { throw 'No active IPv4 adapter available for TV wake' }

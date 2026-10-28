param(
    [Parameter(Mandatory = $true)][string]$ImagePath,
    [string]$Lang = 'zh-Hans-CN',
    [switch]$Json
)

# always emit UTF-8 on stdout, otherwise a GBK console mangles the JSON for the caller
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null

$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

function Await($op, $type) {
    $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    return $task.Result
}

$full = (Resolve-Path $ImagePath).Path
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($full)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = $null
try { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language($Lang))) } catch { }
if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($null -eq $engine) { throw "no OCR engine available" }

"engine lang: $($engine.RecognizerLanguage.LanguageTag)  image: $($bitmap.PixelWidth)x$($bitmap.PixelHeight)"
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

$rows = foreach ($line in $result.Lines) {
    $words = @($line.Words)
    $x = [double]::MaxValue; $y = [double]::MaxValue; $r = 0.0; $b = 0.0
    foreach ($w in $words) {
        $br = $w.BoundingRect
        if ($br.X -lt $x) { $x = $br.X }
        if ($br.Y -lt $y) { $y = $br.Y }
        $right = $br.X + $br.Width
        $bottom = $br.Y + $br.Height
        if ($right -gt $r) { $r = $right }
        if ($bottom -gt $b) { $b = $bottom }
    }
    $text = $line.Text
    # Windows OCR inserts spaces between CJK glyphs; join CJK, keep latin spacing
    $text = [regex]::Replace($text, '(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '')
    $text = [regex]::Replace($text, '(?<=[\u4e00-\u9fff])\s+(?=[0-9A-Za-z])', '')
    $text = [regex]::Replace($text, '(?<=[0-9A-Za-z])\s+(?=[\u4e00-\u9fff])', '')
    if ($x -eq [double]::MaxValue) { $x = 0 }
    [pscustomobject]@{
        X    = [int]$x
        Y    = [int]$y
        W    = [int]($r - $x)
        H    = [int]($b - $y)
        Text = ($text -replace '\s+', ' ').Trim()
    }
}
$rows = $rows | Sort-Object Y, X
if ($Json) {
    [pscustomobject]@{ engine = $engine.RecognizerLanguage.LanguageTag; width = $bitmap.PixelWidth; height = $bitmap.PixelHeight; lines = @($rows) } |
        ConvertTo-Json -Depth 5 -Compress
} else {
    "lines: $($rows.Count)"
    $rows | Format-Table -AutoSize | Out-String -Width 200
}

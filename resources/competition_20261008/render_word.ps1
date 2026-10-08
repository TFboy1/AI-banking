$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$rootDir = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$outputDir = Join-Path $rootDir 'outputs\bankmate_参赛材料_20261008'
$qaDir = Join-Path $PSScriptRoot 'qa'
$wordApp = $null
try {
    $wordApp = New-Object -ComObject Word.Application
    $wordApp.Visible = $false
    $wordApp.DisplayAlerts = 0
    $wordApp.AutomationSecurity = 3
    foreach ($file in (Get-ChildItem -LiteralPath $outputDir -Filter '*.docx' | Sort-Object Name)) {
        $document = $null
        $docQaDir = Join-Path $qaDir $file.Name.Substring(0, 2)
        New-Item -ItemType Directory -Path $docQaDir -Force | Out-Null
        $pdfPath = Join-Path $docQaDir ($file.BaseName + '.pdf')
        try {
            $document = $wordApp.Documents.Open($file.FullName, $false, $true, $false)
            $document.Repaginate()
            $pages = $document.ComputeStatistics(2)
            $document.ExportAsFixedFormat($pdfPath, 17)
            Write-Output ($file.Name + ': ' + $pages + ' pages')
        } finally {
            if ($null -ne $document) {
                $document.Close(0)
                [System.Runtime.InteropServices.Marshal]::ReleaseComObject($document) | Out-Null
            }
        }
    }
} finally {
    if ($null -ne $wordApp) {
        $wordApp.Quit()
        [System.Runtime.InteropServices.Marshal]::ReleaseComObject($wordApp) | Out-Null
    }
}

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$dest = Join-Path $root 'app_data/models'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
$model = Join-Path $dest 'face_detection_yunet.onnx'
$expected = '8F2383E4DD3CFBB4553EA8718107FC0423210DC964F9F4280604804ED2552FA4'
$commit = '47534e27c9851bb1128ccc0102f1145e27f23f98'
if (-not (Test-Path -LiteralPath $model)) {
    Invoke-WebRequest "https://media.githubusercontent.com/media/opencv/opencv_zoo/$commit/models/face_detection_yunet/face_detection_yunet_2023mar.onnx" -OutFile $model
}
if ((Get-FileHash -LiteralPath $model -Algorithm SHA256).Hash -ne $expected) { throw 'YuNet model hash mismatch' }
Invoke-WebRequest "https://raw.githubusercontent.com/opencv/opencv_zoo/$commit/models/face_detection_yunet/LICENSE" -OutFile (Join-Path $dest 'YuNet-LICENSE')

#Requires -Version 5.1
param(
    [Parameter(Mandatory=$true)]
    [string]$VideoPath,

    [Parameter(Mandatory=$false)]
    [double]$StartTime = -1,

    [Parameter(Mandatory=$false)]
    [double]$Duration = 3,

    [Parameter(Mandatory=$false)]
    [switch]$SkipMetadata,

    [Parameter(Mandatory=$false)]
    [string]$FramesDir = '',

    [Parameter(Mandatory=$false)]
    [string]$OutputDir = '',

    [Parameter(Mandatory=$false)]
    [switch]$Simplified
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $VideoPath)) {
    Write-Error "File not found: $VideoPath"
    exit 1
}

$videoName = [System.IO.Path]::GetFileNameWithoutExtension($VideoPath)
$videoDir = Split-Path $VideoPath -Parent
$outDir = if ($OutputDir) {
    [System.IO.Path]::GetFullPath($OutputDir)
} else {
    Join-Path $videoDir $videoName
}
if (-not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

Write-Host "Processing: $VideoPath" -ForegroundColor Cyan
Write-Host "Output directory: $outDir" -ForegroundColor Cyan

# ---- frame output directory ----
$endTime = $StartTime + $Duration
$frameOutDir = if ($FramesDir) { $FramesDir } else { Join-Path $outDir "frames_${StartTime}s-${endTime}s" }
$frameHashCsv = $null
$gopIdx = 0
$gopMinFrameCount = $null
$gopMaxFrameCount = $null
$gopAvgFrameCount = $null
$gopMinKeyInterval = $null
$gopMaxKeyInterval = $null
$gopAvgKeyInterval = $null
$gopPictTypeSummary = $null
$gopPatternSamples = @()
$totalSteps = 2
$currentStep = 0
if (-not $SkipMetadata) { $totalSteps += 5 }
if ($StartTime -ge 0) { $totalSteps += 1 }
$frameExportFirstPtsTime = $null
$frameExportStartFrameIdx = -1
$frameExportEndFrameIdx = -1
$frameExportActualStartRel = $null
$frameExportActualEndRel = $null

# ---- 1. MD5 ----
$currentStep++
Write-Host "[$currentStep/$totalSteps] Calculating MD5..." -ForegroundColor Yellow
$md5 = (Get-FileHash -Path $VideoPath -Algorithm MD5).Hash

if ($SkipMetadata) {
    # metadata skipped, go directly to frame steps
} else {
    # ---- 2. ffprobe JSON metadata ----
    $currentStep++
    Write-Host "[$currentStep/$totalSteps] Extracting metadata..." -ForegroundColor Yellow
    $jsonRaw = & ffprobe -v quiet -print_format json -show_format -show_streams $VideoPath 2>&1
    $meta = $jsonRaw | ConvertFrom-Json
    $vStream = $meta.streams | Where-Object { $_.codec_type -eq 'video' } | Select-Object -First 1
    $aStream = $meta.streams | Where-Object { $_.codec_type -eq 'audio' } | Select-Object -First 1
    $fmt = $meta.format
    $allStreams = $meta.streams

    # ---- 3. Frame data CSV ----
    $currentStep++
    Write-Host "[$currentStep/$totalSteps] Exporting frame data..." -ForegroundColor Yellow
    $frameCsv = Join-Path $outDir "frames.csv"

    $frameHeader = 'frame_index,key_frame,pts_time,duration_time,pkt_pos,pkt_size,pict_type,interlaced_frame'
    $sw = [System.IO.StreamWriter]::new($frameCsv, $false, [System.Text.UTF8Encoding]::new($true))
    $sw.WriteLine($frameHeader)

    $frameIdx = 0
    $framesJsonRaw = & ffprobe -v quiet -select_streams v:0 -show_frames `
        -show_entries frame=pts_time,pict_type,key_frame,pkt_size,pkt_pos,duration_time,interlaced_frame `
        -print_format json $VideoPath 2>&1
    $framesMeta = $framesJsonRaw | ConvertFrom-Json
    foreach ($fr in $framesMeta.frames) {
        $keyFrame = if ($null -ne $fr.key_frame) { $fr.key_frame } else { '' }
        $ptsTime = if ($null -ne $fr.pts_time) { $fr.pts_time } else { '' }
        $frameDuration = if ($null -ne $fr.duration_time) { $fr.duration_time } else { '' }
        $pktPos = if ($null -ne $fr.pkt_pos) { $fr.pkt_pos } else { '' }
        $pktSize = if ($null -ne $fr.pkt_size) { $fr.pkt_size } else { '' }
        $pictType = if ($null -ne $fr.pict_type) { $fr.pict_type } else { '' }
        $interlaced = if ($null -ne $fr.interlaced_frame) { $fr.interlaced_frame } else { '' }
        $sw.WriteLine("$frameIdx,$keyFrame,$ptsTime,$frameDuration,$pktPos,$pktSize,$pictType,$interlaced")
        $frameIdx++
    }
    $sw.Dispose()
    Write-Host "  Exported $frameIdx frames" -ForegroundColor Green

    # ---- 4. GOP structure ----
    $currentStep++
    Write-Host "[$currentStep/$totalSteps] Analyzing GOP structure..." -ForegroundColor Yellow

    $frameRowsForGop = @(Import-Csv -LiteralPath $frameCsv)
    $gopRows = [System.Collections.ArrayList]::new()
    $currentGop = $null

    foreach ($fr in $frameRowsForGop) {
        $fi = [int]$fr.frame_index
        $isKey = ([string]$fr.key_frame -eq '1')
        $isBoundary = $isKey -or ($null -eq $currentGop)

        if ($isBoundary) {
            if ($null -ne $currentGop) {
                $last = $currentGop.frames[-1]
                $pattern = (($currentGop.frames | ForEach-Object { [string]$_.pict_type }) -join '')
                $gopDuration = ''
                if ($currentGop.start_pts_time -ne '' -and $last.pts_time -ne '') {
                    $lastDuration = 0.0
                    try { $lastDuration = [double]$last.duration_time } catch { $lastDuration = 0.0 }
                    $gopDuration = ([double]$last.pts_time + $lastDuration) - [double]$currentGop.start_pts_time
                }
                [void]$gopRows.Add([pscustomobject]@{
                    gop_index = $gopRows.Count
                    start_frame = $currentGop.start_frame
                    end_frame = [int]$last.frame_index
                    frame_count = $currentGop.frames.Count
                    start_pts_time = $currentGop.start_pts_time
                    end_pts_time = $last.pts_time
                    duration_time = $gopDuration
                    key_frame_interval = $currentGop.frames.Count
                    pattern = $pattern
                    i_count = @($currentGop.frames | Where-Object { $_.pict_type -eq 'I' }).Count
                    p_count = @($currentGop.frames | Where-Object { $_.pict_type -eq 'P' }).Count
                    b_count = @($currentGop.frames | Where-Object { $_.pict_type -eq 'B' }).Count
                    other_count = @($currentGop.frames | Where-Object { $_.pict_type -notin @('I','P','B') }).Count
                })
            }
            $currentGop = [pscustomobject]@{
                start_frame = $fi
                start_pts_time = $fr.pts_time
                frames = [System.Collections.ArrayList]::new()
            }
        }

        [void]$currentGop.frames.Add($fr)
    }

    if ($null -ne $currentGop -and $currentGop.frames.Count -gt 0) {
        $last = $currentGop.frames[-1]
        $pattern = (($currentGop.frames | ForEach-Object { [string]$_.pict_type }) -join '')
        $gopDuration = ''
        if ($currentGop.start_pts_time -ne '' -and $last.pts_time -ne '') {
            $lastDuration = 0.0
            try { $lastDuration = [double]$last.duration_time } catch { $lastDuration = 0.0 }
            $gopDuration = ([double]$last.pts_time + $lastDuration) - [double]$currentGop.start_pts_time
        }
        [void]$gopRows.Add([pscustomobject]@{
            gop_index = $gopRows.Count
            start_frame = $currentGop.start_frame
            end_frame = [int]$last.frame_index
            frame_count = $currentGop.frames.Count
            start_pts_time = $currentGop.start_pts_time
            end_pts_time = $last.pts_time
            duration_time = $gopDuration
            key_frame_interval = $currentGop.frames.Count
            pattern = $pattern
            i_count = @($currentGop.frames | Where-Object { $_.pict_type -eq 'I' }).Count
            p_count = @($currentGop.frames | Where-Object { $_.pict_type -eq 'P' }).Count
            b_count = @($currentGop.frames | Where-Object { $_.pict_type -eq 'B' }).Count
            other_count = @($currentGop.frames | Where-Object { $_.pict_type -notin @('I','P','B') }).Count
        })
    }

    $gopIdx = $gopRows.Count
    if ($gopIdx -gt 0) {
        $gopFrameCounts = @($gopRows | ForEach-Object { [int]$_.frame_count })
        $gopMinFrameCount = ($gopFrameCounts | Measure-Object -Minimum).Minimum
        $gopMaxFrameCount = ($gopFrameCounts | Measure-Object -Maximum).Maximum
        $gopAvgFrameCount = [math]::Round(($gopFrameCounts | Measure-Object -Average).Average, 3)
        $gopKeyIntervals = @($gopRows | ForEach-Object { [int]$_.key_frame_interval })
        $gopMinKeyInterval = ($gopKeyIntervals | Measure-Object -Minimum).Minimum
        $gopMaxKeyInterval = ($gopKeyIntervals | Measure-Object -Maximum).Maximum
        $gopAvgKeyInterval = [math]::Round(($gopKeyIntervals | Measure-Object -Average).Average, 3)
        $gopTotalI = ($gopRows | ForEach-Object { [int]$_.i_count } | Measure-Object -Sum).Sum
        $gopTotalP = ($gopRows | ForEach-Object { [int]$_.p_count } | Measure-Object -Sum).Sum
        $gopTotalB = ($gopRows | ForEach-Object { [int]$_.b_count } | Measure-Object -Sum).Sum
        $gopTotalOther = ($gopRows | ForEach-Object { [int]$_.other_count } | Measure-Object -Sum).Sum
        $gopPictTypeSummary = "I=$gopTotalI, P=$gopTotalP, B=$gopTotalB, Other=$gopTotalOther"
        $gopPatternSamples = @(
            $gopRows |
                Select-Object -First 3 |
                ForEach-Object {
                    "GOP#$($_.gop_index): frame $($_.start_frame)-$($_.end_frame), $($_.frame_count) frames, pattern=$($_.pattern)"
                }
        )
    }
    Write-Host "  Analyzed $gopIdx GOP records" -ForegroundColor Green

    # ---- 5. Packet data CSV ----
    $currentStep++
    Write-Host "[$currentStep/$totalSteps] Exporting packet data..." -ForegroundColor Yellow
    $packetCsv = Join-Path $outDir "packets.csv"

    $packetHeader = 'packet_index,stream_index,pts,pts_time,dts,dts_time,duration,duration_time,size,pos,flags'
    $sw2 = [System.IO.StreamWriter]::new($packetCsv, $false, [System.Text.UTF8Encoding]::new($true))
    $sw2.WriteLine($packetHeader)

    $packetIdx = 0
    & ffprobe -v quiet -select_streams v:0 -show_packets `
        -show_entries packet=stream_index,pts,pts_time,dts,dts_time,duration,duration_time,size,pos,flags `
        -of csv=print_section=0 $VideoPath 2>&1 | ForEach-Object {
        $line = $_.Trim()
        if ($line) {
            $sw2.WriteLine("$packetIdx,$line")
            $packetIdx++
        }
    }
    $sw2.Dispose()
    Write-Host "  Exported $packetIdx packets" -ForegroundColor Green
}

# ---- 5. Frame hash CSV (SHA256) ----
# Always run, independent of -SkipMetadata
$fpsForHash = 25
if ($meta -and $vStream -and $vStream.r_frame_rate) {
    $rfps = $vStream.r_frame_rate
    if ($rfps -match '^(\d+)/(\d+)$') {
        $fpsForHash = [math]::Round([int]$Matches[1] / [int]$Matches[2], 3)
    } elseif ($rfps -match '^\d+(\.\d+)?$') {
        $fpsForHash = [double]$rfps
    }
}

$currentStep++
Write-Host "[$currentStep/$totalSteps] Extracting frame SHA256 hashes..." -ForegroundColor Yellow
$frameHashCsv = Join-Path $outDir "framehash.csv"

$swHash = [System.IO.StreamWriter]::new($frameHashCsv, $false, [System.Text.UTF8Encoding]::new($true))
$swHash.WriteLine('frame_index,dts,pts,pts_time,decoded_size,sha256')

$hashIdx = 0
$ffmpegHashOutput = & ffmpeg -nostdin -hide_banner -loglevel error -i $VideoPath -f framehash -hash sha256 - 2>$null
$ffmpegHashOutput | ForEach-Object {
    $line = $_.Trim()
    if ($line -and $line -notmatch '^#') {
        # framehash format: stream_index, dts, pts, duration, size, hash
        $parts = $line -split ',\s*'
        if ($parts.Count -ge 6 -and $parts[0] -eq '0') {
            $dts = $parts[1].Trim()
            $pts = $parts[2].Trim()
            $ptime = [math]::Round([double]$pts / $fpsForHash, 4)
            $size = $parts[4].Trim()
            $hash = $parts[5].Trim()
            $swHash.WriteLine("$hashIdx,$dts,$pts,$ptime,$size,$hash")
            $hashIdx++
        }
    }
}
$swHash.Dispose()
Write-Host "  Exported $hashIdx frame hashes" -ForegroundColor Green

# ---- 6. Frame images (PNG) ----
if ($StartTime -ge 0) {
    $currentStep++
    Write-Host "[$currentStep/$totalSteps] Exporting frame images (${StartTime}s - ${endTime}s)..." -ForegroundColor Yellow

    if (-not (Test-Path $frameOutDir)) {
        New-Item -ItemType Directory -Path $frameOutDir -Force | Out-Null
    }

    # StartTime is relative to the first decoded video frame. Some files have
    # non-zero video PTS, so do not use "ffmpeg -ss $StartTime" directly.
    # Locate the relative time window by first_pts_time + StartTime, then
    # export by frame index to keep filenames and pictures aligned.
    $startFrameIdx = -1
    $endFrameIdx = -1
    $actualStartRel = $null
    $actualEndRel = $null
    $firstPtsTimeForExport = $null
    $epsilon = 0.000001

    if ($frameCsv -and (Test-Path $frameCsv)) {
        $csvData = Import-Csv $frameCsv
        $firstPtsTime = [double]$csvData[0].pts_time
        $firstPtsTimeForExport = $firstPtsTime
        $targetStartPtsTime = $firstPtsTime + $StartTime
        $targetEndPtsTime = $firstPtsTime + $StartTime + $Duration
        $targetRows = @($csvData | Where-Object {
            $pts = [double]$_.pts_time
            $pts -ge ($targetStartPtsTime - $epsilon) -and $pts -lt ($targetEndPtsTime - $epsilon)
        })
        if ($targetRows.Count -gt 0) {
            $firstTarget = $targetRows | Select-Object -First 1
            $lastTarget = $targetRows | Select-Object -Last 1
            $startFrameIdx = [int]$firstTarget.frame_index
            $endFrameIdx = [int]$lastTarget.frame_index
            $actualStartRel = [double]$firstTarget.pts_time - $firstPtsTime
            $actualEndRel = [double]$lastTarget.pts_time - $firstPtsTime
        }
    }

    if ($startFrameIdx -lt 0 -or $endFrameIdx -lt $startFrameIdx) {
        # Fallback for -SkipMetadata: scan frame PTS and map the requested
        # relative window to absolute PTS before exporting by frame index.
        $script:scanIdx = 0
        $script:firstPts = $null
        $script:startIdx = -1
        $script:endIdx = -1
        $script:startRel = $null
        $script:endRel = $null
        & ffprobe -v quiet -select_streams v:0 -show_frames -show_entries frame=pts_time -of csv=print_section=0 $VideoPath 2>&1 | ForEach-Object {
            $line = $_.Trim()
            if ($line) {
                $pts = [double]$line
                if ($null -eq $script:firstPts) { $script:firstPts = $pts }
                $targetStartPts = $script:firstPts + $StartTime
                $targetEndPts = $script:firstPts + $StartTime + $Duration
                if ($pts -ge ($targetStartPts - $epsilon) -and $pts -lt ($targetEndPts - $epsilon)) {
                    if ($script:startIdx -lt 0) {
                        $script:startIdx = $script:scanIdx
                        $script:startRel = $pts - $script:firstPts
                    }
                    $script:endIdx = $script:scanIdx
                    $script:endRel = $pts - $script:firstPts
                }
                $script:scanIdx++
            }
        }
        $firstPtsTimeForExport = $script:firstPts
        $startFrameIdx = $script:startIdx
        $endFrameIdx = $script:endIdx
        $actualStartRel = $script:startRel
        $actualEndRel = $script:endRel
    }

    if ($startFrameIdx -lt 0 -or $endFrameIdx -lt $startFrameIdx) {
        Write-Error "No video frames found for relative time range ${StartTime}s-${endTime}s."
        exit 1
    }

    Get-ChildItem -LiteralPath $frameOutDir -Filter 'temp_*.png' -ErrorAction SilentlyContinue | Remove-Item -Force
    Get-ChildItem -LiteralPath $frameOutDir -Filter 'frame_*.png' -ErrorAction SilentlyContinue | Remove-Item -Force

    $selectFilter = "select=between(n\,$startFrameIdx\,$endFrameIdx),setpts=N/FRAME_RATE/TB"
    $framePattern = Join-Path $frameOutDir 'frame_%06d.png'
    $frameExportLog = Join-Path $frameOutDir 'ffmpeg_frame_export.log'
    $frameExportStdoutLog = Join-Path $frameOutDir 'ffmpeg_frame_export.stdout.log'
    $ffmpegArgs = @(
        '-nostdin', '-y', '-hide_banner', '-loglevel', 'error',
        '-i', $VideoPath,
        '-vf', $selectFilter,
        '-vsync', '0',
        '-start_number', [string]$startFrameIdx,
        $framePattern
    )
    & ffmpeg @ffmpegArgs 1> $frameExportStdoutLog 2> $frameExportLog
    $ffmpegExitCode = $LASTEXITCODE

    $pngCount = @(Get-ChildItem -LiteralPath $frameOutDir -Filter 'frame_*.png').Count
    $expectedCount = $endFrameIdx - $startFrameIdx + 1
    if ($ffmpegExitCode -ne 0 -and $pngCount -ne $expectedCount) {
        Write-Error "ffmpeg frame export failed with exit code $ffmpegExitCode. See $frameExportLog"
        exit 1
    }
    if ($pngCount -ne $expectedCount) {
        Write-Warning "Expected $expectedCount PNG frames, but exported $pngCount."
    }

    $frameExportFirstPtsTime = $firstPtsTimeForExport
    $frameExportStartFrameIdx = $startFrameIdx
    $frameExportEndFrameIdx = $endFrameIdx
    $frameExportActualStartRel = $actualStartRel
    $frameExportActualEndRel = $actualEndRel

    $actualRangeText = if ($null -ne $actualStartRel -and $null -ne $actualEndRel) {
        "rel_pts_time $([math]::Round($actualStartRel, 3))-$([math]::Round($actualEndRel, 3))s"
    } else {
        "relative time ${StartTime}s-${endTime}s"
    }
    Write-Host "  Exported $pngCount PNG frames (frame_index ${startFrameIdx}-${endFrameIdx}, $actualRangeText) to $frameOutDir" -ForegroundColor Green
}

# ---- Summary info file (only if not skipping metadata) ----
if (-not $SkipMetadata) {
    $currentStep++
    Write-Host "[$currentStep/$totalSteps] Generating summary..." -ForegroundColor Yellow
    $infoFile = Join-Path $outDir "info.txt"

    # --- helper functions ---
    function fmt-Size($bytes) {
        $mb = [math]::Round($bytes / 1048576.0, 2)
        return "$('{0:N0}' -f $bytes) 字节 (~$mb MB)"
    }

    function fmt-Duration($sec) {
        if ($null -eq $sec -or [string]::IsNullOrWhiteSpace([string]$sec)) {
            return '-'
        }
        $d = [double]$sec
        $totalSeconds = [int][math]::Round($d, 0, [System.MidpointRounding]::AwayFromZero)
        $h = [math]::Floor($totalSeconds / 3600)
        $m = [math]::Floor(($totalSeconds % 3600) / 60)
        $s = $totalSeconds % 60

        if ($h -gt 0) {
            return "~${h}h${m}min${s}s"
        } elseif ($m -gt 0) {
            return "~${m}min${s}s"
        } else {
            return "~${s}s"
        }
    }

    function fmt-Fps($rateStr) {
        if ($rateStr -match '^(\d+)/(\d+)$') {
            $numerator = [int]$Matches[1]
            $denominator = [int]$Matches[2]
            if ($denominator -eq 0) { return $rateStr }
            return [math]::Round($numerator / $denominator, 3)
        }
        return $rateStr
    }

    function fmt-Bitrate($bps) {
        $b = [int]$bps
        if ($b -ge 1000000) {
            return "$('{0:N0}' -f $b) bps (~$([math]::Round($b/1000000, 2)) Mbps)"
        }
        return "$('{0:N0}' -f $b) bps (~$([math]::Round($b/1000)) kbps)"
    }

    function safe-Val($obj, $prop) {
        if ($obj -and $obj.PSObject.Properties[$prop]) { return $obj.$prop }
        return '-'
    }

    function to-DoubleOrNull($value) {
        if ($null -eq $value) { return $null }
        $text = [string]$value
        if ([string]::IsNullOrWhiteSpace($text) -or $text -eq '-') { return $null }
        try { return [double]$text } catch { return $null }
    }

    $timebaseFirstPts = $null
    if ($frameCsv -and (Test-Path $frameCsv)) {
        $firstFrameForTimebase = Import-Csv -LiteralPath $frameCsv | Select-Object -First 1
        if ($firstFrameForTimebase) {
            $timebaseFirstPts = to-DoubleOrNull $firstFrameForTimebase.pts_time
        }
    }
    if ($null -eq $timebaseFirstPts) {
        $timebaseFirstPts = to-DoubleOrNull (safe-Val $vStream 'start_time')
    }
    $timebaseFormatStart = to-DoubleOrNull (safe-Val $fmt 'start_time')
    $timebaseVideoStreamStart = to-DoubleOrNull (safe-Val $vStream 'start_time')
    $timebaseAudioStreamStart = to-DoubleOrNull (safe-Val $aStream 'start_time')
    $timebaseOffset = $null
    $timebaseMismatch = $false
    if ($null -ne $timebaseFirstPts -and $null -ne $timebaseFormatStart) {
        $timebaseOffset = $timebaseFirstPts - $timebaseFormatStart
        $timebaseMismatch = [math]::Abs($timebaseOffset) -gt 0.5
    }

    # --- build output lines ---
    $infoLines = [System.Collections.ArrayList]::new()

    [void]$infoLines.Add('============================================================')
    [void]$infoLines.Add('  视频文件信息')
    if ($Simplified) {
        [void]$infoLines.Add('  [简化模式：仅输出 info.txt 和帧图片，CSV 文件未保留]')
    }
    [void]$infoLines.Add("  文件名:    $(Split-Path $VideoPath -Leaf)")
    [void]$infoLines.Add("  生成时间:  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
    [void]$infoLines.Add("  输出目录:  $outDir")
    [void]$infoLines.Add('============================================================')
    [void]$infoLines.Add('')

    # -- 基本信息 --
    [void]$infoLines.Add('[基本信息]')
    [void]$infoLines.Add("  MD5 哈希:       $md5")
    [void]$infoLines.Add("  文件大小:        $(fmt-Size ([long]$fmt.size))")
    [void]$infoLines.Add("  完整路径:        $VideoPath")
    [void]$infoLines.Add('')

    # -- 容器信息 --
    [void]$infoLines.Add('[容器信息]')
    [void]$infoLines.Add("  容器格式:        $(safe-Val $fmt 'format_long_name')")
    [void]$infoLines.Add("  格式简称:        $(safe-Val $fmt 'format_name')")
    [void]$infoLines.Add("  流数量:          $(safe-Val $fmt 'nb_streams')")
    [void]$infoLines.Add("  节目数量:        $(safe-Val $fmt 'nb_programs')")
    [void]$infoLines.Add("  数据流组数量:    $(safe-Val $fmt 'nb_stream_groups')")
    [void]$infoLines.Add("  探测得分:        $(safe-Val $fmt 'probe_score')")
    [void]$infoLines.Add("  起始时间:        $(safe-Val $fmt 'start_time') 秒")
    [void]$infoLines.Add("  播放时长:        $(safe-Val $fmt 'duration') 秒（$(fmt-Duration $fmt.duration)）")

    if ($fmt.tags) {
        [void]$infoLines.Add("  [容器标签]")
        if ($fmt.tags.major_brand)       { [void]$infoLines.Add("    Major Brand:      $($fmt.tags.major_brand)") }
        if ($fmt.tags.minor_version)     { [void]$infoLines.Add("    Minor Version:    $($fmt.tags.minor_version)") }
        if ($fmt.tags.compatible_brands) { [void]$infoLines.Add("    Compatible Brands: $($fmt.tags.compatible_brands)") }
        if ($fmt.tags.encoder)           { [void]$infoLines.Add("    编码器:           $($fmt.tags.encoder)") }
        if ($fmt.tags.creation_time)     { [void]$infoLines.Add("    创建时间:         $($fmt.tags.creation_time)") }
    }
    [void]$infoLines.Add('')

    # -- 时间基准说明 --
    [void]$infoLines.Add('[时间基准说明]')
    if ($null -ne $timebaseFirstPts) {
        [void]$infoLines.Add("  视频首帧 PTS:    $('{0:F6}' -f $timebaseFirstPts) 秒")
    } else {
        [void]$infoLines.Add('  视频首帧 PTS:    -')
    }
    if ($null -ne $timebaseFormatStart) {
        [void]$infoLines.Add("  容器起始时间:    $('{0:F6}' -f $timebaseFormatStart) 秒")
    } else {
        [void]$infoLines.Add('  容器起始时间:    -')
    }
    if ($null -ne $timebaseVideoStreamStart) {
        [void]$infoLines.Add("  视频流起始时间:  $('{0:F6}' -f $timebaseVideoStreamStart) 秒")
    } else {
        [void]$infoLines.Add('  视频流起始时间:  -')
    }
    if ($null -ne $timebaseAudioStreamStart) {
        [void]$infoLines.Add("  音频流起始时间:  $('{0:F6}' -f $timebaseAudioStreamStart) 秒")
    } else {
        [void]$infoLines.Add('  音频流起始时间:  -')
    }
    if ($null -ne $timebaseOffset) {
        [void]$infoLines.Add("  视频-容器偏移:   $('{0:F6}' -f $timebaseOffset) 秒")
    } else {
        [void]$infoLines.Add('  视频-容器偏移:   -')
    }
    [void]$infoLines.Add('  分析基准:        后续漂移、时间轴一致性、帧图导出均以视频流首帧为 0 秒。')
    [void]$infoLines.Add('  换算公式:        video_rel = pts_time - first_pts_time')
    if ($timebaseMismatch) {
        [void]$infoLines.Add('  注意:            容器/音频起始时间与视频首帧不一致；播放器显示时间可能与视频首帧相对时间相差上述偏移量。')
        [void]$infoLines.Add('                  容器相对时间仅用于说明差异，不能替代视频流首帧基准。')
    } else {
        [void]$infoLines.Add('  注意:            未见明显容器起始时间与视频首帧起始时间偏移。')
    }
    [void]$infoLines.Add('')

    # -- 媒体流信息 (all streams) --
    $streamIdx = 0
    foreach ($st in $allStreams) {
        $stType = safe-Val $st 'codec_type'
        $stTypeCN = switch ($stType) {
            'video' { '视频流' }
            'audio' { '音频流' }
            'subtitle' { '字幕流' }
            'data' { '数据流' }
            default { "${stType}流" }
        }
        [void]$infoLines.Add("[媒体流 #$streamIdx - $stTypeCN]")
        [void]$infoLines.Add("  编码类型:        $stType")
        [void]$infoLines.Add("  编码名称:        $(safe-Val $st 'codec_name')")
        [void]$infoLines.Add("  编码全称:        $(safe-Val $st 'codec_long_name')")
        [void]$infoLines.Add("  编码 Profile:    $(safe-Val $st 'profile')")
        [void]$infoLines.Add("  编码 Tag:        $(safe-Val $st 'codec_tag_string') ($(safe-Val $st 'codec_tag'))")
        [void]$infoLines.Add("  MIME 编码串:     $(safe-Val $st 'mime_codec_string')")
        [void]$infoLines.Add("  流 ID:           $(safe-Val $st 'id')")
        [void]$infoLines.Add("  流索引:          $(safe-Val $st 'index')")
        [void]$infoLines.Add("  时间基准:        $(safe-Val $st 'time_base')")
        [void]$infoLines.Add("  起始时间:        $(safe-Val $st 'start_time') 秒")
        [void]$infoLines.Add("  起始 PTS:        $(safe-Val $st 'start_pts')")
        [void]$infoLines.Add("  持续时长:        $(safe-Val $st 'duration') 秒（$(fmt-Duration $st.duration)）")
        [void]$infoLines.Add("  持续 PTS:        $(safe-Val $st 'duration_ts')")

        if ($stType -eq 'video') {
            [void]$infoLines.Add("  像素格式:        $(safe-Val $st 'pix_fmt')")
            [void]$infoLines.Add("  分辨率:          $(safe-Val $st 'width') x $(safe-Val $st 'height')")
            [void]$infoLines.Add("  编码后尺寸:      $(safe-Val $st 'coded_width') x $(safe-Val $st 'coded_height')")
            [void]$infoLines.Add("  SAR:             $(safe-Val $st 'sample_aspect_ratio')")
            [void]$infoLines.Add("  DAR:             $(safe-Val $st 'display_aspect_ratio')")
            [void]$infoLines.Add("  色彩范围:        $(safe-Val $st 'color_range')")
            [void]$infoLines.Add("  色彩空间:        $(safe-Val $st 'color_space')")
            [void]$infoLines.Add("  色彩传递:        $(safe-Val $st 'color_transfer')")
            [void]$infoLines.Add("  色彩原色:        $(safe-Val $st 'color_primaries')")
            [void]$infoLines.Add("  色度位置:        $(safe-Val $st 'chroma_location')")
            [void]$infoLines.Add("  场序:            $(safe-Val $st 'field_order')")
            [void]$infoLines.Add("  含 B 帧:         $(safe-Val $st 'has_b_frames')")
            [void]$infoLines.Add("  参考帧数:        $(safe-Val $st 'refs')")
            [void]$infoLines.Add("  AVC 编码:        $(safe-Val $st 'is_avc')")
            [void]$infoLines.Add("  NAL 长度大小:    $(safe-Val $st 'nal_length_size')")
            [void]$infoLines.Add("  Level:           $(safe-Val $st 'level')")
            [void]$infoLines.Add("  位深/像素:       $(safe-Val $st 'bits_per_raw_sample')")
            [void]$infoLines.Add("  Extradata 大小:  $(safe-Val $st 'extradata_size')")
            [void]$infoLines.Add("  标称帧率:        $(safe-Val $st 'r_frame_rate') ($(fmt-Fps $st.r_frame_rate) fps)")
            [void]$infoLines.Add("  平均帧率:        $(safe-Val $st 'avg_frame_rate') ($(fmt-Fps $st.avg_frame_rate) fps)")
            [void]$infoLines.Add("  总帧数:          $(safe-Val $st 'nb_frames')")
            [void]$infoLines.Add("  已读帧数:        $(safe-Val $st 'nb_read_frames')")
            [void]$infoLines.Add("  已读包数:        $(safe-Val $st 'nb_read_packets')")
            if ($st.bit_rate) { [void]$infoLines.Add("  码率:            $(fmt-Bitrate $st.bit_rate)") }
            if ($st.max_bit_rate) { [void]$infoLines.Add("  最大码率:        $(fmt-Bitrate $st.max_bit_rate)") }
        }

        if ($stType -eq 'audio') {
            [void]$infoLines.Add("  采样格式:        $(safe-Val $st 'sample_fmt')")
            [void]$infoLines.Add("  采样率:          $(safe-Val $st 'sample_rate') Hz")
            [void]$infoLines.Add("  声道数:          $(safe-Val $st 'channels')")
            [void]$infoLines.Add("  声道布局:        $(safe-Val $st 'channel_layout')")
            if ($st.bit_rate) { [void]$infoLines.Add("  码率:            $(fmt-Bitrate $st.bit_rate)") }
        }

        # disposition
        if ($st.disposition) {
            $disp = $st.disposition
            $activeAttrs = @()
            foreach ($prop in $disp.PSObject.Properties) {
                if ($prop.Value -eq 1) { $activeAttrs += $prop.Name }
            }
            if ($activeAttrs.Count -gt 0) {
                [void]$infoLines.Add("  Disposition:     $($activeAttrs -join ', ')")
            }
        }

        # stream tags
        if ($st.tags) {
            [void]$infoLines.Add("  [流标签]")
            if ($st.tags.language)      { [void]$infoLines.Add("    语言:           $($st.tags.language)") }
            if ($st.tags.handler_name)  { [void]$infoLines.Add("    处理器名称:     $($st.tags.handler_name)") }
            if ($st.tags.vendor_id)     { [void]$infoLines.Add("    厂商 ID:        $($st.tags.vendor_id)") }
            if ($st.tags.encoder)       { [void]$infoLines.Add("    编码器:         $($st.tags.encoder)") }
            if ($st.tags.creation_time) { [void]$infoLines.Add("    创建时间:       $($st.tags.creation_time)") }
        }

        [void]$infoLines.Add('')
        $streamIdx++
    }

    # -- 总体码率 --
    [void]$infoLines.Add('[总体码率]')
    if ($fmt.bit_rate) {
        [void]$infoLines.Add("  总码率:          $(fmt-Bitrate $fmt.bit_rate)")
    }
    if ($vStream -and $vStream.bit_rate) {
        [void]$infoLines.Add("  视频码率:        $(fmt-Bitrate $vStream.bit_rate)")
    }
    if ($aStream -and $aStream.bit_rate) {
        [void]$infoLines.Add("  音频码率:        $(fmt-Bitrate $aStream.bit_rate)")
    }
    [void]$infoLines.Add('')

    # -- 帧数据 --
    [void]$infoLines.Add('[帧数据]')
    if ($Simplified) {
        [void]$infoLines.Add('  输出文件:        [简化模式未保留 CSV，仅在此 TXT 中记录摘要]')
    } else {
        [void]$infoLines.Add("  输出文件:        frames.csv")
    }
    [void]$infoLines.Add('  字段:            frame_index, key_frame, pts_time, duration_time,')
    [void]$infoLines.Add('                  pkt_pos, pkt_size, pict_type, interlaced_frame')
    [void]$infoLines.Add("  总帧数:          $frameIdx")
    [void]$infoLines.Add('')

    # -- GOP 数据 --
    [void]$infoLines.Add('[GOP 数据]')
    [void]$infoLines.Add('  输出方式:        不单独生成 GOP 表格；仅在本 TXT 中记录 GOP 结构摘要。')
    [void]$infoLines.Add("  GOP 数量:        $gopIdx")
    if ($null -ne $gopMinFrameCount -and $null -ne $gopMaxFrameCount) {
        [void]$infoLines.Add("  GOP 帧数范围:    $gopMinFrameCount - $gopMaxFrameCount 帧")
        [void]$infoLines.Add("  GOP 平均帧数:    $gopAvgFrameCount 帧")
    }
    if ($null -ne $gopMinKeyInterval -and $null -ne $gopMaxKeyInterval) {
        [void]$infoLines.Add("  关键帧间隔范围:  $gopMinKeyInterval - $gopMaxKeyInterval 帧")
        [void]$infoLines.Add("  平均关键帧间隔:  $gopAvgKeyInterval 帧")
    }
    if ($gopPictTypeSummary) {
        [void]$infoLines.Add("  I/P/B 统计:      $gopPictTypeSummary")
    }
    if ($gopPatternSamples.Count -gt 0) {
        [void]$infoLines.Add('  GOP 结构样例:')
        foreach ($sample in $gopPatternSamples) {
            [void]$infoLines.Add("                  $sample")
        }
    }
    [void]$infoLines.Add('  说明:            GOP 由关键帧 key_frame=1 划分；首段从视频首帧开始。')
    [void]$infoLines.Add('')

    # -- 包数据 --
    [void]$infoLines.Add('[包数据]')
    if ($Simplified) {
        [void]$infoLines.Add('  输出文件:        [简化模式未保留 CSV，仅在此 TXT 中记录摘要]')
    } else {
        [void]$infoLines.Add("  输出文件:        packets.csv")
    }
    [void]$infoLines.Add('  字段:            packet_index, stream_index, pts, pts_time, dts,')
    [void]$infoLines.Add('                  dts_time, duration, duration_time, size, pos, flags')
    [void]$infoLines.Add("  总包数:          $packetIdx")
    [void]$infoLines.Add('')

    # -- 帧哈希 --
    [void]$infoLines.Add('[帧哈希]')
    if ($Simplified) {
        [void]$infoLines.Add('  输出文件:        [简化模式未保留 CSV，仅在此 TXT 中记录摘要]')
    } else {
        [void]$infoLines.Add("  输出文件:        framehash.csv")
    }
    [void]$infoLines.Add('  字段:            frame_index, dts, pts, pts_time, decoded_size, sha256')
    [void]$infoLines.Add("  总帧数:          $hashIdx")
    [void]$infoLines.Add('')

    # -- 帧画面 --
    if ($StartTime -ge 0) {
        [void]$infoLines.Add('[帧画面]')
        [void]$infoLines.Add("  时间区间:        ${StartTime}s - ${endTime}s (持续 $Duration 秒)")
        if ($null -ne $frameExportFirstPtsTime) {
            [void]$infoLines.Add("  首帧 PTS:        $('{0:F6}' -f $frameExportFirstPtsTime) 秒")
            [void]$infoLines.Add("  PTS 换算:        target_pts_time = first_pts_time + relative_time")
            [void]$infoLines.Add("  帧号区间:        $frameExportStartFrameIdx - $frameExportEndFrameIdx")
        }
        if ($null -ne $frameExportActualStartRel -and $null -ne $frameExportActualEndRel) {
            [void]$infoLines.Add("  实际相对时间:    $('{0:F3}' -f $frameExportActualStartRel) 秒 - $('{0:F3}' -f $frameExportActualEndRel) 秒")
        }
        [void]$infoLines.Add("  输出目录:        $frameOutDir")
        [void]$infoLines.Add('  格式:            PNG')
        [void]$infoLines.Add("  总帧数:          $pngCount")
        [void]$infoLines.Add('')
    }

    [void]$infoLines.Add('============================================================')

    $infoLines | Out-File -FilePath $infoFile -Encoding utf8
}

if ($Simplified) {
    # 简化模式：只保留 info.txt 和帧图片，删除所有 CSV 文件
    Write-Host "Simplified mode: cleaning up CSV files..." -ForegroundColor Yellow
    if ($frameCsv -and (Test-Path $frameCsv)) {
        Remove-Item -LiteralPath $frameCsv -Force
        Write-Host "  Removed: $frameCsv" -ForegroundColor Gray
    }
    if ($packetCsv -and (Test-Path $packetCsv)) {
        Remove-Item -LiteralPath $packetCsv -Force
        Write-Host "  Removed: $packetCsv" -ForegroundColor Gray
    }
    if ($frameHashCsv -and (Test-Path $frameHashCsv)) {
        Remove-Item -LiteralPath $frameHashCsv -Force
        Write-Host "  Removed: $frameHashCsv" -ForegroundColor Gray
    }
}

Write-Host ""
Write-Host "Done!" -ForegroundColor Green
Write-Host "  Output directory: $outDir"
if (-not $SkipMetadata) {
    Write-Host "  $infoFile"
    if (-not $Simplified) {
        Write-Host "  $frameCsv"
        Write-Host "  $packetCsv"
    }
}
if (-not $Simplified) {
    Write-Host "  $frameHashCsv"
}
if ($StartTime -ge 0) {
    Write-Host "  $frameOutDir ($pngCount PNG files)"
}

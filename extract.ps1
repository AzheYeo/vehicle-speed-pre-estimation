#Requires -Version 5.1
param(
    [Parameter(Mandatory=$true)]
    [string]$VideoPath,

    [Parameter(Mandatory=$false)]
    [float]$StartTime = -1,

    [Parameter(Mandatory=$false)]
    [float]$Duration = 4,

    [Parameter(Mandatory=$false)]
    [switch]$SkipMetadata,

    [Parameter(Mandatory=$false)]
    [string]$FramesDir = '',

    [Parameter(Mandatory=$false)]
    [string]$OutputDir = ''
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $VideoPath)) {
    Write-Error "File not found: $VideoPath"
    exit 1
}

$videoName = [System.IO.Path]::GetFileNameWithoutExtension($VideoPath)
$videoDir = Split-Path $VideoPath -Parent
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
$outDir = if ($OutputDir) {
    [System.IO.Path]::GetFullPath($OutputDir)
} else {
    Join-Path $videoDir "$videoName`_video_metadata_$ts"
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
$totalSteps = 2
$currentStep = 0
if (-not $SkipMetadata) { $totalSteps += 3 }
if ($StartTime -ge 0) { $totalSteps += 1 }

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
    $frameCsv = Join-Path $outDir "$videoName`_frames_$ts.csv"

    $frameHeader = 'frame_index,key_frame,pts_time,duration_time,pkt_pos,pkt_size,pict_type,interlaced_frame'
    $sw = [System.IO.StreamWriter]::new($frameCsv, $false, [System.Text.UTF8Encoding]::new($true))
    $sw.WriteLine($frameHeader)

    $frameIdx = 0
    & ffprobe -v quiet -select_streams v:0 -show_frames `
        -show_entries frame=pts_time,pict_type,key_frame,pkt_size,pkt_pos,duration_time,interlaced_frame `
        -of csv=print_section=0 $VideoPath 2>&1 | ForEach-Object {
        $line = $_.Trim()
        if ($line) {
            $sw.WriteLine("$frameIdx,$line")
            $frameIdx++
        }
    }
    $sw.Dispose()
    Write-Host "  Exported $frameIdx frames" -ForegroundColor Green

    # ---- 4. Packet data CSV ----
    $currentStep++
    Write-Host "[$currentStep/$totalSteps] Exporting packet data..." -ForegroundColor Yellow
    $packetCsv = Join-Path $outDir "$videoName`_packets_$ts.csv"

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
$frameHashCsv = Join-Path $outDir "$videoName`_framehash_$ts.csv"

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

    # Step 6a: Extract frames to temp files
    $tempPattern = Join-Path $frameOutDir 'temp_%06d.png'
    & ffmpeg -nostdin -hide_banner -loglevel error -ss $StartTime -t $Duration -i $VideoPath -vsync 0 "$tempPattern" 2>&1

    # Step 6b: Determine starting frame_index for the time range
    $startFrameIdx = -1
    if ($frameCsv -and (Test-Path $frameCsv)) {
        # Read from already-generated frame CSV (preferred: includes frame_index)
        $csvData = Import-Csv $frameCsv
        $firstPtsTime = [double]$csvData[0].pts_time
        $targetPtsTime = $firstPtsTime + $StartTime
        $firstTarget = $csvData | Where-Object { [double]$_.pts_time -ge ($targetPtsTime - 0.000001) } | Select-Object -First 1
        if ($firstTarget) { $startFrameIdx = [int]$firstTarget.frame_index }
    }

    if ($startFrameIdx -lt 0) {
        # Fallback: count frames before start time by scanning ffprobe output
        $script:beforeCount = 0
        $script:firstPts = $null
        & ffprobe -v quiet -select_streams v:0 -show_frames -show_entries frame=pts_time -of csv=print_section=0 $VideoPath 2>&1 | ForEach-Object {
            $line = $_.Trim()
            if ($line) {
                $pts = [double]$line
                if ($null -eq $script:firstPts) { $script:firstPts = $pts }
                if ($pts -lt ($script:firstPts + $StartTime - 0.000001)) { $script:beforeCount++ }
            }
        }
        $startFrameIdx = $script:beforeCount
    }

    # Step 6c: Rename temp files to frame_<frame_index>.png
    $pngCount = 0
    $tempFiles = @(Get-ChildItem -Path $frameOutDir -Filter 'temp_*.png' | Sort-Object Name)
    $idx = $startFrameIdx
    foreach ($tf in $tempFiles) {
        $newName = "frame_{0:D6}.png" -f $idx
        Rename-Item -Path $tf.FullName -NewName $newName
        $idx++
        $pngCount++
    }

    Write-Host "  Exported $pngCount PNG frames (frame_index ${startFrameIdx}-$($idx - 1), ${StartTime}s-${endTime}s) to $frameOutDir" -ForegroundColor Green
}

# ---- Summary info file (only if not skipping metadata) ----
if (-not $SkipMetadata) {
    $currentStep++
    Write-Host "[$currentStep/$totalSteps] Generating summary..." -ForegroundColor Yellow
    $infoFile = Join-Path $outDir "$videoName`_info_$ts.txt"

    # --- helper functions ---
    function fmt-Size($bytes) {
        $mb = [math]::Round($bytes / 1048576.0, 2)
        return "$('{0:N0}' -f $bytes) 字节 (~$mb MB)"
    }

    function fmt-Duration($sec) {
        $d = [double]$sec
        if ($d -ge 3600) {
            $h = [math]::Floor($d / 3600)
            $m = [math]::Floor(($d - $h * 3600) / 60)
            $s = [math]::Round($d - $h * 3600 - $m * 60, 3)
            return "$h 时 $m 分 $s 秒"
        } elseif ($d -gt 60) {
            $m = [math]::Floor($d / 60)
            $s = [math]::Round($d - $m * 60, 3)
            return "$m 分 $s 秒"
        } else {
            return "$d 秒"
        }
    }

    function fmt-Fps($rateStr) {
        if ($rateStr -match '^(\d+)/(\d+)$') {
            return [math]::Round([int]$Matches[1] / [int]$Matches[2], 3)
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

    # --- build output lines ---
    $infoLines = [System.Collections.ArrayList]::new()

    [void]$infoLines.Add('============================================================')
    [void]$infoLines.Add('  视频文件信息')
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
    [void]$infoLines.Add("  播放时长:        $(safe-Val $fmt 'duration') 秒 ($(fmt-Duration $fmt.duration))")

    if ($fmt.tags) {
        [void]$infoLines.Add("  [容器标签]")
        if ($fmt.tags.major_brand)       { [void]$infoLines.Add("    Major Brand:      $($fmt.tags.major_brand)") }
        if ($fmt.tags.minor_version)     { [void]$infoLines.Add("    Minor Version:    $($fmt.tags.minor_version)") }
        if ($fmt.tags.compatible_brands) { [void]$infoLines.Add("    Compatible Brands: $($fmt.tags.compatible_brands)") }
        if ($fmt.tags.encoder)           { [void]$infoLines.Add("    编码器:           $($fmt.tags.encoder)") }
        if ($fmt.tags.creation_time)     { [void]$infoLines.Add("    创建时间:         $($fmt.tags.creation_time)") }
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
        [void]$infoLines.Add("  持续时长:        $(safe-Val $st 'duration') 秒 ($(fmt-Duration $st.duration))")
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
    [void]$infoLines.Add("  输出文件:        $videoName`_frames_$ts.csv")
    [void]$infoLines.Add('  字段:            frame_index, key_frame, pts_time, duration_time,')
    [void]$infoLines.Add('                  pkt_pos, pkt_size, pict_type, interlaced_frame')
    [void]$infoLines.Add("  总帧数:          $frameIdx")
    [void]$infoLines.Add('')

    # -- 包数据 --
    [void]$infoLines.Add('[包数据]')
    [void]$infoLines.Add("  输出文件:        $videoName`_packets_$ts.csv")
    [void]$infoLines.Add('  字段:            packet_index, stream_index, pts, pts_time, dts,')
    [void]$infoLines.Add('                  dts_time, duration, duration_time, size, pos, flags')
    [void]$infoLines.Add("  总包数:          $packetIdx")
    [void]$infoLines.Add('')

    # -- 帧哈希 --
    [void]$infoLines.Add('[帧哈希]')
    [void]$infoLines.Add("  输出文件:        $videoName`_framehash_$ts.csv")
    [void]$infoLines.Add('  字段:            frame_index, dts, pts, pts_time, decoded_size, sha256')
    [void]$infoLines.Add("  总帧数:          $hashIdx")
    [void]$infoLines.Add('')

    # -- 帧画面 --
    if ($StartTime -ge 0) {
        [void]$infoLines.Add('[帧画面]')
        [void]$infoLines.Add("  时间区间:        ${StartTime}s - ${endTime}s (持续 $Duration 秒)")
        [void]$infoLines.Add("  输出目录:        $frameOutDir")
        [void]$infoLines.Add('  格式:            PNG')
        [void]$infoLines.Add("  总帧数:          $pngCount")
        [void]$infoLines.Add('')
    }

    [void]$infoLines.Add('============================================================')

    $infoLines | Out-File -FilePath $infoFile -Encoding utf8
}

Write-Host ""
Write-Host "Done!" -ForegroundColor Green
Write-Host "  Output directory: $outDir"
if (-not $SkipMetadata) {
    Write-Host "  $infoFile"
    Write-Host "  $frameCsv"
    Write-Host "  $packetCsv"
}
Write-Host "  $frameHashCsv"
if ($StartTime -ge 0) {
    Write-Host "  $frameOutDir ($pngCount PNG files)"
}

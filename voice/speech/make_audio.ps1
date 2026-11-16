# Synthesizes the Stage 1 test clips with the built-in Windows SAPI voices, as 16 kHz mono 16-bit WAV.
param([string]$OutDir = $(if ($env:VOICE_HOME) { "$env:VOICE_HOME\evidence\stage1\audio" } else { "$HOME\voice\evidence\stage1\audio" }))

Add-Type -AssemblyName System.Speech
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)

$clips = [ordered]@{
	"single"     = @("Microsoft Zira Desktop", "What time is it in Tokyo right now?")
	"two_request" = @("Microsoft David Desktop", "Find last week's sales report and then email a summary to Dana.")
	"correction" = @("Microsoft Zira Desktop", "Set a timer for ten minutes, no wait, make that fifteen minutes.")
	"long"       = @("Microsoft David Desktop", "I need help planning tomorrow. First, check my calendar for any meetings before noon. Then find the slides from the quarterly review and send them to the whole team. After that, remind me at three o'clock to call the dentist, and finally, order more coffee for the office.")
}

foreach ($name in $clips.Keys) {
	$voice, $text = $clips[$name]
	$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
	$s.SelectVoice($voice)
	$path = Join-Path $OutDir "$name.wav"
	$s.SetOutputToWaveFile($path, $format)
	$s.Speak($text)
	$s.Dispose()
	Set-Content -Path (Join-Path $OutDir "$name.txt") -Value $text -NoNewline
	Write-Output "$name -> $path"
}

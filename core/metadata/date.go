package metadata

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const (
	DateAccuracyJSON     = 1
	DateAccuracyFilename = 2
	DateAccuracyExif     = 3
	DateAccuracyNone     = 99
)

type datePattern struct {
	re    *regexp.Regexp
	parse func(string) (time.Time, bool)
}

var datePatterns = []datePattern{
	// Screenshot_20190919-053857.jpg
	{regexp.MustCompile(`(?i)(20|19|18)\d{2}(0[1-9]|1[0-2])[0-3]\d-\d{6}`), parseLayout("20060102-150405")},
	// IMG_20190509_154733.jpg
	{regexp.MustCompile(`(?i)(20|19|18)\d{2}(0[1-9]|1[0-2])[0-3]\d_\d{6}`), parseLayout("20060102_150405")},
	// Screenshot_2019-04-16-11-19-37.jpg
	{regexp.MustCompile(`(?i)(20|19|18)\d{2}-(0[1-9]|1[0-2])-[0-3]\d-\d{2}-\d{2}-\d{2}`), parseLayout("2006-01-02-15-04-05")},
	// signal-2020-10-26-163832.jpg
	{regexp.MustCompile(`(?i)(20|19|18)\d{2}-(0[1-9]|1[0-2])-[0-3]\d-\d{6}`), parseLayout("2006-01-02-150405")},
	// 201801261147521000.jpg (use first 14 digits)
	{regexp.MustCompile(`(?i)(20|19|18)\d{2}(0[1-9]|1[0-2])[0-3]\d\d{7,}`), parseDigitsFirst14()},
	// 2016_01_30_11_49_15.mp4
	{regexp.MustCompile(`(?i)(20|19|18)\d{2}_(0[1-9]|1[0-2])_[0-3]\d_\d{2}_\d{2}_\d{2}`), parseLayout("2006_01_02_15_04_05")},
	// WhatsApp: IMG-20201231-WA0001.jpg / VID-20201231-WA0001.mp4
	{regexp.MustCompile(`(?i)(IMG|VID)-\d{8}-WA\d+`), parseWhatsApp()},
	// Snapchat: Snapchat-1699999999.jpg (Unix seconds)
	{regexp.MustCompile(`(?i)Snapchat-(\d{10})`), parseSnapchatUnix()},
	// Snapchat edited: Snapchat-1699999999-edited.jpg (Unix seconds)
	{regexp.MustCompile(`(?i)Snapchat-(\d{10})-edited`), parseSnapchatUnix()},
	// Snapchat: Snapchat-1699999999999.jpg (Unix milliseconds)
	{regexp.MustCompile(`(?i)Snapchat-(\d{13})`), parseSnapchatUnixMillis()},
	// Snapchat edited: Snapchat-1699999999999-edited.jpg (Unix milliseconds)
	{regexp.MustCompile(`(?i)Snapchat-(\d{13})-edited`), parseSnapchatUnixMillis()},
	// Pixel: PXL_20210102_123456.jpg
	{regexp.MustCompile(`(?i)PXL_\d{8}_\d{6}`), parseLayout("PXL_20060102_150405")},
	// Pixel with millis: PXL_20210102_123456789.jpg (take first 6 after date)
	{regexp.MustCompile(`(?i)PXL_\d{8}_\d{9}`), parsePixelMillis()},
	// Android: IMG_20210102_123456.jpg / VID_20210102_123456.mp4
	{regexp.MustCompile(`(?i)(IMG|VID)_\d{8}_\d{6}`), parseLayout("IMG_20060102_150405")},
}

// GuessDateFromFilename tries to extract a date from the file name.
func GuessDateFromFilename(path string) (time.Time, bool) {
	base := filepath.Base(path)
	for _, pat := range datePatterns {
		match := pat.re.FindString(base)
		if match == "" {
			continue
		}
		if t, ok := pat.parse(match); ok {
			return t, true
		}
	}
	return time.Time{}, false
}

// ParseJSONTakenTime extracts the photoTakenTime timestamp from a Google Photos JSON file.
func ParseJSONTakenTime(jsonPath string) (time.Time, bool) {
	if jsonPath == "" {
		return time.Time{}, false
	}
	data, err := os.ReadFile(jsonPath)
	if err != nil {
		return time.Time{}, false
	}

	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err != nil {
		return time.Time{}, false
	}

	photoTaken, ok := raw["photoTakenTime"].(map[string]any)
	if !ok {
		return time.Time{}, false
	}
	tsRaw, ok := photoTaken["timestamp"]
	if !ok {
		return time.Time{}, false
	}

	ts, ok := parseTimestamp(tsRaw)
	if !ok {
		return time.Time{}, false
	}
	return time.Unix(ts, 0), true
}

// ExtractBestDate chooses the best available date, preferring JSON unless
// a filename date is older and looks reasonable.
func ExtractBestDate(srcPath, jsonPath string) (time.Time, int, bool) {
	jsonTime, hasJSON := ParseJSONTakenTime(jsonPath)
	fileTime, hasFile := GuessDateFromFilename(srcPath)
	exifTime, hasExif := ParseExifTakenTime(srcPath)

	if hasJSON && hasFile {
		if shouldOverrideJSON(jsonTime, fileTime) {
			return fileTime, DateAccuracyFilename, true
		}
		return jsonTime, DateAccuracyJSON, true
	}
	if hasJSON {
		return jsonTime, DateAccuracyJSON, true
	}
	if hasFile {
		return fileTime, DateAccuracyFilename, true
	}
	if hasExif {
		return exifTime, DateAccuracyExif, true
	}
	return time.Time{}, DateAccuracyNone, false
}

func shouldOverrideJSON(jsonTime, fileTime time.Time) bool {
	if !fileTime.Before(jsonTime) {
		return false
	}
	if !isReasonable(fileTime) {
		return false
	}
	// If filename is older, prefer it as original capture time.
	return true
}

func isReasonable(t time.Time) bool {
	if t.IsZero() {
		return false
	}
	year := t.Year()
	now := time.Now()
	if year < 1990 {
		return false
	}
	if year > now.Year()+1 {
		return false
	}
	return true
}

func parseTimestamp(v any) (int64, bool) {
	switch val := v.(type) {
	case string:
		ts, err := strconv.ParseInt(strings.TrimSpace(val), 10, 64)
		if err != nil {
			return 0, false
		}
		return ts, true
	case float64:
		return int64(val), true
	default:
		return 0, false
	}
}

func parseLayout(layout string) func(string) (time.Time, bool) {
	return func(s string) (time.Time, bool) {
		return ParseWithLayout(layout, s)
	}
}

func parseDigitsFirst14() func(string) (time.Time, bool) {
	return func(s string) (time.Time, bool) {
		digits := regexp.MustCompile(`\d+`).FindString(s)
		if len(digits) < 14 {
			return time.Time{}, false
		}
		digits = digits[:14]
		t, err := time.ParseInLocation("20060102150405", digits, time.Local)
		if err != nil {
			return time.Time{}, false
		}
		return t, true
	}
}

func parseWhatsApp() func(string) (time.Time, bool) {
	return func(s string) (time.Time, bool) {
		re := regexp.MustCompile(`(?i)(IMG|VID)-(\d{8})-WA\d+`)
		m := re.FindStringSubmatch(s)
		if len(m) < 3 {
			return time.Time{}, false
		}
		t, err := time.ParseInLocation("20060102", m[2], time.Local)
		if err != nil {
			return time.Time{}, false
		}
		return t, true
	}
}

func parsePixelMillis() func(string) (time.Time, bool) {
	return func(s string) (time.Time, bool) {
		re := regexp.MustCompile(`(?i)PXL_(\d{8})_(\d{9})`)
		m := re.FindStringSubmatch(s)
		if len(m) < 3 {
			return time.Time{}, false
		}
		// Use first 6 digits for HHMMSS.
		timePart := m[2]
		if len(timePart) < 6 {
			return time.Time{}, false
		}
		ts := fmt.Sprintf("PXL_%s_%s", m[1], timePart[:6])
		return parseLayout("PXL_20060102_150405")(ts)
	}
}

func parseSnapchatUnix() func(string) (time.Time, bool) {
	return func(s string) (time.Time, bool) {
		re := regexp.MustCompile(`(?i)Snapchat-(\d{10})`)
		m := re.FindStringSubmatch(s)
		if len(m) < 2 {
			return time.Time{}, false
		}
		return ParseWithLayout("UNIX", m[1])
	}
}

func parseSnapchatUnixMillis() func(string) (time.Time, bool) {
	return func(s string) (time.Time, bool) {
		re := regexp.MustCompile(`(?i)Snapchat-(\d{13})`)
		m := re.FindStringSubmatch(s)
		if len(m) < 2 {
			return time.Time{}, false
		}
		return ParseWithLayout("UNIXMS", m[1])
	}
}

func ParseWithLayout(layout, value string) (time.Time, bool) {
	switch strings.ToUpper(strings.TrimSpace(layout)) {
	case "UNIXMS":
		ms, err := strconv.ParseInt(strings.TrimSpace(value), 10, 64)
		if err != nil {
			return time.Time{}, false
		}
		return time.Unix(0, ms*int64(time.Millisecond)), true
	case "UNIX":
		sec, err := strconv.ParseInt(strings.TrimSpace(value), 10, 64)
		if err != nil {
			return time.Time{}, false
		}
		return time.Unix(sec, 0), true
	default:
		t, err := time.ParseInLocation(layout, value, time.Local)
		if err != nil {
			return time.Time{}, false
		}
		return t, true
	}
}

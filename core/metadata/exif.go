package metadata

import (
	"encoding/json"
	"os/exec"
	"strings"
	"sync"
	"time"
)

type exifResult struct {
	DateTimeOriginal string `json:"DateTimeOriginal"`
	CreateDate       string `json:"CreateDate"`
	MediaCreateDate  string `json:"MediaCreateDate"`
	TrackCreateDate  string `json:"TrackCreateDate"`
}

var (
	exiftoolOnce      sync.Once
	exiftoolAvailable bool
)

func hasExiftool() bool {
	exiftoolOnce.Do(func() {
		if _, err := exec.LookPath("exiftool"); err == nil {
			exiftoolAvailable = true
		}
	})
	return exiftoolAvailable
}

func ParseExifTakenTime(path string) (time.Time, bool) {
	if path == "" {
		return time.Time{}, false
	}
	if !hasExiftool() {
		return time.Time{}, false
	}

	out, err := exec.Command(
		"exiftool",
		"-j",
		"-DateTimeOriginal",
		"-CreateDate",
		"-MediaCreateDate",
		"-TrackCreateDate",
		"-d",
		"%Y-%m-%dT%H:%M:%S%z",
		path,
	).Output()
	if err != nil {
		return time.Time{}, false
	}

	var rows []exifResult
	if err := json.Unmarshal(out, &rows); err != nil {
		return time.Time{}, false
	}
	if len(rows) == 0 {
		return time.Time{}, false
	}

	for _, v := range []string{
		rows[0].DateTimeOriginal,
		rows[0].CreateDate,
		rows[0].MediaCreateDate,
		rows[0].TrackCreateDate,
	} {
		if t, ok := parseExifTime(v); ok {
			return t, true
		}
	}
	return time.Time{}, false
}

func parseExifTime(value string) (time.Time, bool) {
	value = strings.TrimSpace(value)
	if value == "" || strings.Contains(value, "0000-00-00") {
		return time.Time{}, false
	}
	layouts := []string{
		"2006-01-02T15:04:05-0700",
		"2006-01-02T15:04:05",
		"2006:01:02 15:04:05",
	}
	for _, layout := range layouts {
		if t, err := time.Parse(layout, value); err == nil {
			return t, true
		}
	}
	return time.Time{}, false
}

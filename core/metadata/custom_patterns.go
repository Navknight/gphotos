package metadata

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"time"
)

type CustomPattern struct {
	Regex  string `json:"regex"`
	Layout string `json:"layout"`
}

func LoadCustomPatterns(path string) ([]CustomPattern, error) {
	if path == "" {
		return nil, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var patterns []CustomPattern
	if err := json.Unmarshal(data, &patterns); err != nil {
		return nil, err
	}
	return patterns, nil
}

func SaveCustomPatterns(path string, patterns []CustomPattern) error {
	if path == "" {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(patterns, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o644)
}

func GuessDateFromFilenameWithCustomAndExclusions(path string, custom []CustomPattern, exclude map[string]bool) (time.Time, bool) {
	if isExcluded(path, exclude) {
		return time.Time{}, false
	}
	if t, ok := guessWithPatterns(path, buildCustomPatterns(custom)); ok {
		return t, true
	}
	if isExcluded(path, exclude) {
		return time.Time{}, false
	}
	return GuessDateFromFilename(path)
}

func ExtractBestDateWithCustomAndExclusions(srcPath string, jsonTime time.Time, hasJSON bool, custom []CustomPattern, exclude map[string]bool) (time.Time, int, bool, time.Time, bool) {
	fileTime, hasFile := GuessDateFromFilenameWithCustomAndExclusions(srcPath, custom, exclude)
	var exifTime time.Time
	var hasExif bool

	if hasJSON && hasFile {
		if shouldOverrideJSON(jsonTime, fileTime) {
			return fileTime, DateAccuracyFilename, true, exifTime, hasExif
		}
		return jsonTime, DateAccuracyJSON, true, exifTime, hasExif
	}
	if hasJSON {
		return jsonTime, DateAccuracyJSON, true, exifTime, hasExif
	}
	if hasFile {
		return fileTime, DateAccuracyFilename, true, exifTime, hasExif
	}
	exifTime, hasExif = ParseExifTakenTime(srcPath)
	if hasExif {
		return exifTime, DateAccuracyExif, true, exifTime, hasExif
	}
	return time.Time{}, DateAccuracyNone, false, exifTime, hasExif
}

func guessWithPatterns(path string, patterns []datePattern) (time.Time, bool) {
	if len(patterns) == 0 {
		return time.Time{}, false
	}
	base := filepath.Base(path)
	for _, pat := range patterns {
		sub := pat.re.FindStringSubmatch(base)
		if len(sub) == 0 {
			continue
		}
		target := sub[0]
		if len(sub) > 1 {
			target = sub[1]
		}
		if t, ok := pat.parse(target); ok {
			return t, true
		}
	}
	return time.Time{}, false
}

func buildCustomPatterns(custom []CustomPattern) []datePattern {
	if len(custom) == 0 {
		return nil
	}
	out := make([]datePattern, 0, len(custom))
	for _, c := range custom {
		if c.Regex == "" || c.Layout == "" {
			continue
		}
		re, err := regexp.Compile(c.Regex)
		if err != nil {
			continue
		}
		out = append(out, datePattern{
			re:    re,
			parse: parseLayout(c.Layout),
		})
	}
	return out
}

func isExcluded(path string, exclude map[string]bool) bool {
	if len(exclude) == 0 {
		return false
	}
	base := filepath.Base(path)
	return exclude[base]
}

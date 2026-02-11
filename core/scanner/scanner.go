package scanner

import (
	"encoding/json"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

type FilePair struct {
	MediaPath string
	JsonPath  string
	Album     string
}

type jsonTitleEntry struct {
	Title string
	Path  string
}

func ScanTakeout(root string, verbose bool) ([]FilePair, error) {
	var pairs []FilePair
	var media []FilePair
	jsonByTitle := make(map[string][]string)
	jsonByKey := make(map[string][]string)
	jsonByDir := make(map[string][]jsonTitleEntry)
	jsonByNorm := make(map[string][]string)
	found := 0

	filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}

		if d.IsDir() {
			return nil
		}

		lower := strings.ToLower(path)

		if strings.HasSuffix(lower, ".json") {
			base := filepath.Base(path)
			if base != "metadata.json" {
				if title, ok := extractJSONTitle(path); ok && title != "" {
					key := strings.ToLower(title)
					jsonByTitle[key] = append(jsonByTitle[key], path)
					dir := filepath.Dir(path)
					jsonByDir[dir] = append(jsonByDir[dir], jsonTitleEntry{
						Title: title,
						Path:  path,
					})
					if norm := normalizeBaseForMatch(stripExt(title)); norm != "" {
						jsonByNorm[norm] = append(jsonByNorm[norm], path)
					}
				}
				if key := normalizeJSONKey(base); key != "" {
					jsonByKey[key] = append(jsonByKey[key], path)
				}
			}
			return nil
		}

		if isMediaFile(lower) {
			album := detectAlbum(root, path)
			media = append(media, FilePair{
				MediaPath: path,
				JsonPath:  "",
				Album:     album,
			})
			found++
			if verbose {
				rel, _ := filepath.Rel(root, path)
				println("Scanned:", rel)
			}
		}

		return nil
	})

	for _, m := range media {
		m.JsonPath = resolveJSONPath(m.MediaPath, jsonByTitle, jsonByKey, jsonByDir, jsonByNorm)
		pairs = append(pairs, m)
	}

	if verbose {
		println("Scan complete. Media files found:", found)
	}
	return pairs, nil
}

func isMediaFile(lowerPath string) bool {
	return strings.HasSuffix(lowerPath, ".jpg") ||
		strings.HasSuffix(lowerPath, ".jpeg") ||
		strings.HasSuffix(lowerPath, ".png") ||
		strings.HasSuffix(lowerPath, ".heic") ||
		strings.HasSuffix(lowerPath, ".mp4") ||
		strings.HasSuffix(lowerPath, ".mov") ||
		strings.HasSuffix(lowerPath, ".m4v") ||
		strings.HasSuffix(lowerPath, ".gif") ||
		strings.HasSuffix(lowerPath, ".webp") ||
		strings.HasSuffix(lowerPath, ".dng") ||
		strings.HasSuffix(lowerPath, ".nef") ||
		strings.HasSuffix(lowerPath, ".mp") ||
		strings.HasSuffix(lowerPath, ".mv") ||
		strings.HasSuffix(lowerPath, ".mp~2") ||
		strings.HasSuffix(lowerPath, ".mp~3")
}

func resolveJSONPath(mediaPath string, jsonByTitle map[string][]string, jsonByKey map[string][]string, jsonByDir map[string][]jsonTitleEntry, jsonByNorm map[string][]string) string {
	base := filepath.Base(mediaPath)
	baseNoExt := stripExt(base)
	baseLower := strings.ToLower(base)
	baseNoExtLower := strings.ToLower(baseNoExt)
	extLower := strings.ToLower(filepath.Ext(base))

	if path := pickCandidate(jsonByTitle[baseLower], base); path != "" {
		return path
	}
	if path := pickCandidate(jsonByTitle[baseNoExtLower], base); path != "" {
		return path
	}
	if extLower == ".mp" {
		if path := pickCandidate(jsonByTitle[strings.ToLower(base+".jpg")], base); path != "" {
			return path
		}
		if path := pickCandidate(jsonByTitle[strings.ToLower(base+".jpeg")], base); path != "" {
			return path
		}
	}

	if path := pickLivePhotoSiblingJSON(mediaPath, jsonByTitle); path != "" {
		return path
	}

	for _, key := range mediaKeys(base) {
		if path := pickCandidate(jsonByKey[key], base); path != "" {
			return path
		}
	}
	if path := pickPrefixCandidate(mediaPath, jsonByDir); path != "" {
		return path
	}
	if norm := normalizeBaseForMatch(baseNoExt); norm != "" {
		if path := pickCandidate(jsonByNorm[norm], base); path != "" {
			return path
		}
	}
	return ""
}

func stripExt(path string) string {
	ext := filepath.Ext(path)
	if ext == "" {
		return path
	}
	return strings.TrimSuffix(path, ext)
}

func matchesMetadataName(filename, base string) bool {
	if base == "" {
		return false
	}
	// Allow: base(.supplemental-metadata|.metadata)?(.json) with optional (n) suffix.
	// Examples:
	//   IMG_123.jpg.json
	//   IMG_123.jpg.supplemental-metadata.json
	//   IMG_123(1).json
	//   IMG_123(1).metadata.json
	pattern := "^" + regexp.QuoteMeta(base) + "(\\([0-9]+\\))?(\\.supplemental-metadata|\\.metadata)?\\.json$"
	re := regexp.MustCompile(pattern)
	return re.MatchString(filename)
}

func normalizeJSONKey(filename string) string {
	if !strings.HasSuffix(filename, ".json") {
		return ""
	}
	name := strings.TrimSuffix(filename, ".json")
	name = strings.TrimRight(name, ".")
	name = stripTrailingIndex(name)

	lower := strings.ToLower(name)
	if idx := strings.Index(lower, ".supp"); idx >= 0 {
		name = name[:idx]
	} else if idx := strings.Index(lower, ".meta"); idx >= 0 {
		name = name[:idx]
	}
	return name
}

func stripTrailingIndex(name string) string {
	re := regexp.MustCompile(`\(\d+\)$`)
	return re.ReplaceAllString(name, "")
}

func mediaKeys(base string) []string {
	var keys []string
	if base == "" {
		return keys
	}
	base = strings.TrimRight(base, ".")
	baseNoExt := stripExt(base)
	keys = append(keys, base, baseNoExt)
	keys = append(keys, stripTrailingIndex(base), stripTrailingIndex(baseNoExt))
	return dedupeKeys(keys)
}

func dedupeKeys(keys []string) []string {
	seen := make(map[string]bool)
	var out []string
	for _, k := range keys {
		k = strings.TrimSpace(k)
		if k == "" || seen[k] {
			continue
		}
		seen[k] = true
		out = append(out, k)
	}
	return out
}

func extractJSONTitle(path string) (string, bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", false
	}
	var payload struct {
		Title string `json:"title"`
	}
	if err := json.Unmarshal(data, &payload); err != nil {
		return "", false
	}
	if payload.Title == "" {
		return "", false
	}
	return payload.Title, true
}

func pickCandidate(candidates []string, base string) string {
	if len(candidates) == 0 {
		return ""
	}
	if len(candidates) == 1 {
		return candidates[0]
	}
	for _, c := range candidates {
		name := filepath.Base(c)
		if matchesMetadataName(name, base) {
			return c
		}
	}
	return candidates[0]
}

func pickLivePhotoSiblingJSON(mediaPath string, jsonByTitle map[string][]string) string {
	ext := strings.ToLower(filepath.Ext(mediaPath))
	if ext != ".mp4" && ext != ".mov" {
		return ""
	}
	baseNoExt := stripExt(filepath.Base(mediaPath))
	baseNoExt = strings.ToLower(baseNoExt)
	// Common live-photo stills next to MP4/MOV
	exts := []string{".heic", ".jpg", ".jpeg", ".png"}
	for _, e := range exts {
		title := baseNoExt + e
		if path := pickCandidate(jsonByTitle[title], title); path != "" {
			return path
		}
	}
	return ""
}

func pickPrefixCandidate(mediaPath string, jsonByDir map[string][]jsonTitleEntry) string {
	dir := filepath.Dir(mediaPath)
	entries := jsonByDir[dir]
	if len(entries) == 0 {
		return ""
	}

	base := filepath.Base(mediaPath)
	baseNoExt := stripExt(base)
	ext := strings.ToLower(filepath.Ext(base))
	baseNoExt = strings.ToLower(baseNoExt)

	best := ""
	bestLen := 0
	for _, entry := range entries {
		title := strings.ToLower(entry.Title)
		if ext == "" {
			continue
		}
		if !strings.HasSuffix(title, ext) {
			continue
		}
		titleBase := strings.TrimSuffix(title, ext)
		if !strings.HasPrefix(titleBase, baseNoExt) {
			continue
		}
		if best == "" || len(titleBase) < bestLen {
			best = entry.Path
			bestLen = len(titleBase)
		}
	}
	return best
}

func normalizeBaseForMatch(base string) string {
	if base == "" {
		return ""
	}
	b := strings.ToLower(strings.TrimSpace(base))
	b = stripTrailingIndex(b)

	// Remove common edit suffixes.
	for _, suffix := range []string{
		"-edited",
		"-collage",
		"-color_pop",
		"-photo_frame",
		"-overlayed",
	} {
		if strings.HasSuffix(b, suffix) {
			b = strings.TrimSuffix(b, suffix)
			break
		}
	}
	return strings.TrimSpace(b)
}

func detectAlbum(root, path string) string {
	rel, _ := filepath.Rel(root, path)
	parts := strings.Split(rel, string(filepath.Separator))

	if len(parts) > 1 {
		if parts[0] == "Google Photos" && len(parts) > 2 {
			if !strings.HasPrefix(parts[1], "Photos from") {
				return parts[1]
			}
			return ""
		}
		if !strings.HasPrefix(parts[0], "Photos from") {
			return parts[0]
		}
	}
	return ""
}

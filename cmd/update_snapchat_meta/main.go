package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"gphotos/core/metadata"
	"gphotos/core/models"
)

type sourceItem struct {
	mediaPath string
	jsonPath  string
}

var mediaExt = map[string]bool{
	".jpg":  true,
	".jpeg": true,
	".png":  true,
	".heic": true,
	".mp4":  true,
	".mov":  true,
	".m4v":  true,
	".gif":  true,
	".webp": true,
	".dng":  true,
	".nef":  true,
	".mp":   true,
	".mv":   true,
	".mp~2": true,
	".mp~3": true,
}

func main() {
	home, _ := os.UserHomeDir()
	defaultTakeout := filepath.Join(home, "Downloads", "Takeout")
	defaultOut := filepath.Join(home, "Downloads", "out")

	takeoutRoot := flag.String("takeout", defaultTakeout, "Path to original Google Takeout root")
	outRoot := flag.String("out", defaultOut, "Path to output folder containing Snapchat files")
	dryRun := flag.Bool("dry-run", false, "Print planned updates without writing metadata")
	flag.Parse()

	if !metadata.CanWriteMeta() && !*dryRun {
		fmt.Println("exiftool not found in PATH; cannot write metadata.")
		os.Exit(1)
	}

	outByKey, outCount, err := collectOutSnapchat(*outRoot)
	if err != nil {
		fmt.Printf("failed to scan out folder: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("Snapchat files in out: %d (%d keys)\n", outCount, len(outByKey))
	if len(outByKey) == 0 {
		fmt.Println("No Snapchat files found in out folder.")
		return
	}

	fmt.Printf("Searching Takeout for matching keys: %s\n", *takeoutRoot)
	index, err := collectTakeoutSources(*takeoutRoot, outByKey)
	if err != nil {
		fmt.Printf("failed to scan takeout: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("Indexed Snapchat sources: %d\n", len(index))

	var total, updated, missing, noMeta, failed int
	for key, outPaths := range outByKey {
		src, ok := index[key]
		if !ok {
			missing += len(outPaths)
			total += len(outPaths)
			continue
		}
		meta := buildMeta(src.mediaPath, src.jsonPath)
		for _, path := range outPaths {
			total++
			if !metadata.HasWritableMeta(meta) {
				noMeta++
				continue
			}
			if *dryRun {
				fmt.Printf("DRY RUN update: %s  <-  %s\n", path, src.mediaPath)
				updated++
				continue
			}
			if err := metadata.WriteMetaToFile(path, meta); err != nil {
				failed++
				fmt.Printf("write failed: %s (%v)\n", path, err)
				continue
			}
			updated++
		}
	}

	fmt.Println("Summary:")
	fmt.Printf("  Snapchat files in out: %d\n", total)
	fmt.Printf("  Updated: %d\n", updated)
	fmt.Printf("  Missing in Takeout index: %d\n", missing)
	fmt.Printf("  No writable metadata: %d\n", noMeta)
	fmt.Printf("  Write failures: %d\n", failed)
}

func buildMeta(srcPath, jsonPath string) models.MetaData {
	var meta models.MetaData
	jsonMeta, hasJSONMeta := metadata.ParseJSONMeta(jsonPath)
	if hasJSONMeta {
		if jsonMeta.HasPhotoTaken {
			meta.TakenTime = jsonMeta.PhotoTakenTime.Format(time.RFC3339)
		} else if jsonMeta.HasCreation {
			meta.TakenTime = jsonMeta.CreationTime.Format(time.RFC3339)
		}
		if jsonMeta.HasCreation {
			meta.CreationTime = jsonMeta.CreationTime.Format(time.RFC3339)
		}
		meta.Description = jsonMeta.Description
		meta.Favorited = jsonMeta.Favorited
		meta.People = append([]string{}, jsonMeta.People...)
		meta.URL = jsonMeta.URL
		meta.AppSource = jsonMeta.AppSource
		meta.Origin = models.GooglePhotosOrigin{
			FromSharedAlbum:          jsonMeta.Origin.FromSharedAlbum,
			WebUpload:                jsonMeta.Origin.WebUpload,
			MobileUpload:             jsonMeta.Origin.MobileUpload,
			MobileUploadDeviceType:   jsonMeta.Origin.MobileUploadDeviceType,
			MobileUploadDeviceFolder: jsonMeta.Origin.MobileUploadDeviceFolder,
			CompositionType:          jsonMeta.Origin.CompositionType,
		}
		if jsonMeta.HasGeo {
			meta.HasGeo = true
			meta.GPSLat = jsonMeta.Geo.Latitude
			meta.GPSLon = jsonMeta.Geo.Longitude
			meta.GPSAlt = jsonMeta.Geo.Altitude
			meta.GPSSpanLat = jsonMeta.Geo.LatitudeSpan
			meta.GPSSpanLon = jsonMeta.Geo.LongitudeSpan
		}
	}
	if meta.TakenTime == "" {
		if exifTime, ok := metadata.ParseExifTakenTime(srcPath); ok {
			meta.TakenTime = exifTime.Format(time.RFC3339)
		}
	}
	return meta
}

func collectOutSnapchat(outRoot string) (map[string][]string, int, error) {
	outByKey := make(map[string][]string)
	count := 0
	err := filepath.WalkDir(outRoot, func(path string, d os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return nil
		}
		if d.IsDir() || !isMedia(path) {
			return nil
		}
		key := snapchatKey(filepath.Base(path))
		if key == "" {
			return nil
		}
		outByKey[key] = append(outByKey[key], path)
		count++
		return nil
	})
	return outByKey, count, err
}

func collectTakeoutSources(takeoutRoot string, wanted map[string][]string) (map[string]sourceItem, error) {
	mediaByKey := make(map[string]string)
	jsonByKey := make(map[string]string)

	err := filepath.WalkDir(takeoutRoot, func(path string, d os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return nil
		}
		if d.IsDir() {
			return nil
		}

		name := filepath.Base(path)
		key := snapchatKey(name)
		if key == "" {
			return nil
		}
		if _, ok := wanted[key]; !ok {
			return nil
		}

		lower := strings.ToLower(path)
		if strings.HasSuffix(lower, ".json") {
			cur := jsonByKey[key]
			// Prefer supplemental sidecars when available.
			if cur == "" || strings.Contains(lower, ".supplemental-metadata.json") {
				jsonByKey[key] = path
			}
			return nil
		}
		if isMedia(path) {
			if _, exists := mediaByKey[key]; !exists {
				mediaByKey[key] = path
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}

	index := make(map[string]sourceItem)
	for key := range wanted {
		mediaPath := mediaByKey[key]
		if mediaPath == "" {
			continue
		}
		index[key] = sourceItem{
			mediaPath: mediaPath,
			jsonPath:  jsonByKey[key],
		}
	}
	return index, nil
}

func isMedia(path string) bool {
	ext := strings.ToLower(filepath.Ext(path))
	return mediaExt[ext]
}

func snapchatKey(name string) string {
	base := strings.ToLower(strings.TrimSpace(name))
	ext := filepath.Ext(base)
	if ext != "" {
		base = strings.TrimSuffix(base, ext)
	}
	base = strings.TrimSuffix(base, ".supplemental-metadata")
	base = strings.TrimSuffix(base, ".metadata")
	for _, mediaExt := range []string{".jpg", ".jpeg", ".png", ".heic", ".heif", ".mp4", ".mov", ".m4v", ".gif", ".webp", ".dng", ".nef"} {
		base = strings.TrimSuffix(base, mediaExt)
	}
	if !strings.HasPrefix(base, "snapchat-") {
		return ""
	}

	rest := base[len("snapchat-"):]
	var digits strings.Builder
	for _, r := range rest {
		if r < '0' || r > '9' {
			break
		}
		digits.WriteRune(r)
	}
	d := digits.String()
	if len(d) < 10 {
		return ""
	}
	return "snapchat-" + d
}

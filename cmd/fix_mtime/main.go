package main

import (
	"flag"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"gphotos/core/metadata"
)

var mediaExt = map[string]bool{
	".jpg":  true,
	".jpeg": true,
	".png":  true,
	".gif":  true,
	".webp": true,
	".heic": true,
	".heif": true,
	".bmp":  true,
	".tif":  true,
	".tiff": true,
	".avif": true,
	".mp4":  true,
	".mov":  true,
	".m4v":  true,
	".avi":  true,
	".mkv":  true,
	".3gp":  true,
	".wmv":  true,
	".mp":   true,
	".mv":   true,
	".mp~2": true,
	".mp~3": true,
	".dng":  true,
	".nef":  true,
}

func main() {
	root := flag.String("root", "", "Root folder to fix mtimes in (required)")
	dryRun := flag.Bool("dry-run", false, "Print changes without applying")
	tolerance := flag.Float64("tolerance", 2, "Skip files whose mtime is already within N seconds of target")
	workers := flag.Int("workers", 8, "Number of parallel workers")
	flag.Parse()

	if *root == "" {
		fmt.Println("--root is required")
		os.Exit(1)
	}

	rootPath, err := filepath.Abs(*root)
	if err != nil {
		fmt.Printf("Invalid root: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("Scanning %s ...\n", rootPath)
	var files []string
	err = filepath.WalkDir(rootPath, func(path string, d os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return nil
		}
		if d.IsDir() {
			return nil
		}
		ext := strings.ToLower(filepath.Ext(path))
		if mediaExt[ext] {
			files = append(files, path)
		}
		return nil
	})
	if err != nil {
		fmt.Printf("Walk error: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("Found %d media files.\n", len(files))
	if len(files) == 0 {
		return
	}

	var (
		updated      int64
		alreadyOK    int64
		noDate       int64
		failed       int64
		fromExif     int64
		fromFilename int64
		processed    int64
	)

	total := int64(len(files))
	jobs := make(chan string, (*workers)*2)
	var wg sync.WaitGroup

	for i := 0; i < *workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for path := range jobs {
				var targetTime time.Time
				var source string

				// Priority 1: EXIF date
				if t, ok := metadata.ParseExifTakenTime(path); ok {
					targetTime = t
					source = "exif"
				}

				// Priority 2: Filename date
				if targetTime.IsZero() {
					if t, ok := metadata.GuessDateFromFilename(path); ok {
						targetTime = t
						source = "filename"
					}
				}

				done := atomic.AddInt64(&processed, 1)
				if done%500 == 0 || done == total {
					pct := float64(done) / float64(total) * 100
					fmt.Printf("\rProgress: %d/%d (%.1f%%) | updated=%d ok=%d no_date=%d",
						done, total, pct,
						atomic.LoadInt64(&updated),
						atomic.LoadInt64(&alreadyOK),
						atomic.LoadInt64(&noDate))
				}

				if targetTime.IsZero() {
					atomic.AddInt64(&noDate, 1)
					continue
				}

				info, err := os.Stat(path)
				if err != nil {
					atomic.AddInt64(&failed, 1)
					continue
				}

				currentMtime := info.ModTime()
				diff := math.Abs(currentMtime.Sub(targetTime).Seconds())
				if diff <= *tolerance {
					atomic.AddInt64(&alreadyOK, 1)
					continue
				}

				if *dryRun {
					fmt.Printf("\n  WOULD SET: %s  %s -> %s  [%s]\n",
						filepath.Base(path),
						currentMtime.Format("2006-01-02 15:04:05"),
						targetTime.Format("2006-01-02 15:04:05"),
						source)
					atomic.AddInt64(&updated, 1)
				} else {
					if err := os.Chtimes(path, targetTime, targetTime); err != nil {
						fmt.Printf("\n  FAILED: %s: %v\n", filepath.Base(path), err)
						atomic.AddInt64(&failed, 1)
						continue
					}
					atomic.AddInt64(&updated, 1)
				}

				switch source {
				case "exif":
					atomic.AddInt64(&fromExif, 1)
				case "filename":
					atomic.AddInt64(&fromFilename, 1)
				}
			}
		}()
	}

	for _, f := range files {
		jobs <- f
	}
	close(jobs)
	wg.Wait()

	fmt.Printf("\n\nDone.\n")
	fmt.Printf("  Updated:         %d (exif: %d, filename: %d)\n", updated, fromExif, fromFilename)
	fmt.Printf("  Already correct: %d\n", alreadyOK)
	fmt.Printf("  No date found:   %d\n", noDate)
	fmt.Printf("  Failed:          %d\n", failed)
}

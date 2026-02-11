package dedup

import (
	"fmt"
	"gphotos/core/models"
	"gphotos/core/scanner"
	"os"
)

func BuildRegistry(pairs []scanner.FilePair, cachePath string, verbose bool, progress func(done, total int)) map[string]*models.Photo {
	registry := make(map[string]*models.Photo)
	cache, _ := LoadHashCache(cachePath)
	total := len(pairs)
	processed := 0
	for _, p := range pairs {
		info, err := os.Stat(p.MediaPath)
		if err != nil {
			continue
		}
		size := info.Size()
		mtime := info.ModTime().UnixNano()
		var hash string
		if entry, ok := cache.Files[p.MediaPath]; ok && entry.Size == size && entry.MtimeNs == mtime && entry.Hash != "" {
			hash = entry.Hash
		}
		var hashErr error
		if hash == "" {
			hash, hashErr = HashFile(p.MediaPath)
		}
		key := hash
		hashError := false
		if hashErr != nil {
			key = "nohash:" + p.MediaPath
			hash = ""
			hashError = true
			fmt.Printf("Hash failed, keeping file: %s (%v)\n", p.MediaPath, hashErr)
		} else if hash != "" {
			cache.Files[p.MediaPath] = hashCacheEntry{
				Size:    size,
				MtimeNs: mtime,
				Hash:    hash,
			}
		}

		photo, exists := registry[key]
		if !exists {
			photo = &models.Photo{
				Hash:      hash,
				HashError: hashError,
				SrcPath:   p.MediaPath,
				JsonPath:  p.JsonPath,
				Albums:    make(map[string]bool),
			}
			registry[key] = photo
		}

		if p.Album != "" {
			photo.Albums[p.Album] = true
		}

		photo.Size = size

		if verbose {
			fmt.Printf("Hashed: %s\n", photo.SrcPath)
		}
		processed++
		if progress != nil {
			progress(processed, total)
		}
	}

	_ = SaveHashCache(cachePath, cache)
	return registry
}

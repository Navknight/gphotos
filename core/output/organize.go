package output

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"

	"gphotos/core/metadata"
	"gphotos/core/models"
)

const (
	libraryFolder = "Library"
	albumsFolder  = "Albums"
)

// OrganizePhotos copies photos into the output folder.
// Photos with FinalAlbum set go into Albums/<FinalAlbum>/.
// Others go into Library/.
func OrganizePhotos(photos []*models.Photo, outRoot string, dryRun bool, verbose bool, workers int, exifBatch int, progress func(done, total int)) error {
	if outRoot == "" {
		return fmt.Errorf("output root is empty")
	}

	libDir := filepath.Join(outRoot, libraryFolder)
	albDir := filepath.Join(outRoot, albumsFolder)

	if !dryRun {
		if err := os.MkdirAll(libDir, 0o755); err != nil {
			return err
		}
		if err := os.MkdirAll(albDir, 0o755); err != nil {
			return err
		}
	}

	total := len(photos)
	if workers < 1 {
		workers = 1
	}
	if exifBatch < 1 {
		exifBatch = 1
	}

	var (
		mu        sync.Mutex
		processed int64
		firstErr  error
	)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	jobs := make(chan *models.Photo, workers*2)
	metaCh := make(chan metadata.WriteItem, workers*4)
	var metaWg sync.WaitGroup

	if !dryRun && metadata.CanWriteMeta() {
		metaWg.Add(1)
		go func() {
			defer metaWg.Done()
			writer, err := metadata.StartBatchWriter()
			if err != nil {
				if verbose {
					fmt.Printf("Metadata writer unavailable: %v\n", err)
				}
				return
			}
			defer writer.Close()

			var batch []metadata.WriteItem
			flush := func() {
				if len(batch) == 0 {
					return
				}
				if err := writer.Write(batch); err != nil && verbose {
					fmt.Printf("Metadata batch failed: %v\n", err)
				}
				batch = batch[:0]
			}
			for item := range metaCh {
				if !metadata.HasWritableMeta(item.Meta) {
					continue
				}
				batch = append(batch, item)
				if len(batch) >= exifBatch {
					flush()
				}
			}
			flush()
		}()
	}

	var wg sync.WaitGroup
	workerFn := func() {
		defer wg.Done()
		for {
			select {
			case <-ctx.Done():
				return
			case p, ok := <-jobs:
				if !ok {
					return
				}
				if p == nil || p.SrcPath == "" {
					continue
				}

				dstDir := libDir
				if strings.TrimSpace(p.FinalAlbum) != "" {
					dstDir = filepath.Join(albDir, sanitizeFolder(p.FinalAlbum))
					if !dryRun {
						if err := os.MkdirAll(dstDir, 0o755); err != nil {
							mu.Lock()
							if firstErr == nil {
								firstErr = err
								cancel()
							}
							mu.Unlock()
							return
						}
					}
				}

				base := filepath.Base(p.SrcPath)
				ext := strings.ToLower(filepath.Ext(base))
				if kind, ok := metadata.DetectFileKind(p.SrcPath); ok {
					if pref := metadata.PreferredExtension(kind); pref != "" && pref != ext {
						base = strings.TrimSuffix(base, ext) + pref
					}
				}
				mu.Lock()
				dstPath, err := uniquePath(dstDir, base, p.Hash)
				mu.Unlock()
				if err != nil {
					mu.Lock()
					if firstErr == nil {
						firstErr = err
						cancel()
					}
					mu.Unlock()
					return
				}

				if dryRun {
					fmt.Printf("DRY RUN: %s -> %s\n", p.SrcPath, dstPath)
				} else {
					if verbose {
						fmt.Printf("Copy: %s -> %s\n", p.SrcPath, dstPath)
					}
					if err := copyFile(p.SrcPath, dstPath); err != nil {
						mu.Lock()
						if firstErr == nil {
							firstErr = err
							cancel()
						}
						mu.Unlock()
						return
					}
					select {
					case metaCh <- metadata.WriteItem{Path: dstPath, Meta: p.Meta}:
					default:
						metaCh <- metadata.WriteItem{Path: dstPath, Meta: p.Meta}
					}
				}

				done := int(atomic.AddInt64(&processed, 1))
				if progress != nil {
					progress(done, total)
				}
			}
		}
	}

	wg.Add(workers)
	for i := 0; i < workers; i++ {
		go workerFn()
	}

	for _, p := range photos {
		select {
		case <-ctx.Done():
			break
		default:
			jobs <- p
		}
	}
	close(jobs)
	wg.Wait()
	close(metaCh)
	metaWg.Wait()

	if firstErr != nil {
		return firstErr
	}

	return nil
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()

	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer func() {
		_ = out.Close()
	}()

	if _, err := io.Copy(out, in); err != nil {
		return err
	}
	return out.Sync()
}

func uniquePath(dir, filename, hash string) (string, error) {
	path := filepath.Join(dir, filename)
	if _, err := os.Stat(path); os.IsNotExist(err) {
		return path, nil
	} else if err != nil {
		return "", err
	}
	fmt.Printf("Name collision detected: %s\n", path)

	ext := filepath.Ext(filename)
	name := strings.TrimSuffix(filename, ext)
	hashPart := ""
	if hash != "" {
		if len(hash) > 8 {
			hashPart = hash[:8]
		} else {
			hashPart = hash
		}
	}

	if hashPart != "" {
		path = filepath.Join(dir, fmt.Sprintf("%s-%s%s", name, hashPart, ext))
		if _, err := os.Stat(path); os.IsNotExist(err) {
			fmt.Printf("Resolved collision with hash: %s\n", path)
			return path, nil
		} else if err != nil {
			return "", err
		}
	}

	for i := 1; i < 10000; i++ {
		path = filepath.Join(dir, fmt.Sprintf("%s-%d%s", name, i, ext))
		if _, err := os.Stat(path); os.IsNotExist(err) {
			fmt.Printf("Resolved collision with suffix: %s\n", path)
			return path, nil
		} else if err != nil {
			return "", err
		}
	}

	return "", fmt.Errorf("too many name collisions for %s", filename)
}

func sanitizeFolder(name string) string {
	name = strings.TrimSpace(name)
	name = strings.ReplaceAll(name, string(os.PathSeparator), "_")
	if name == "" {
		return "Untitled"
	}
	return name
}

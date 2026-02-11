package dedup

import (
	"fmt"
	"gphotos/core/models"
	"sort"
)

func GroupIdentical(photos []*models.Photo) map[string][]*models.Photo {
	sizeGroups := make(map[int64][]*models.Photo)
	finalGroups := make(map[string][]*models.Photo)

	for _, p := range photos {
		sizeGroups[p.Size] = append(sizeGroups[p.Size], p)
	}

	for size, group := range sizeGroups {
		if len(group) == 1 {
			key := fmt.Sprintf("%dbytes", size)
			finalGroups[key] = group
			continue
		}

		hashGroups := make(map[string][]*models.Photo)
		for _, p := range group {
			if p.HashError {
				key := fmt.Sprintf("nohash:%d:%s", size, p.SrcPath)
				hashGroups[key] = append(hashGroups[key], p)
				continue
			}
			if p.Hash == "" {
				h, err := HashFile(p.SrcPath)
				if err != nil {
					p.HashError = true
					key := fmt.Sprintf("nohash:%d:%s", size, p.SrcPath)
					hashGroups[key] = append(hashGroups[key], p)
					continue
				}
				p.Hash = h
			}
			hashGroups[p.Hash] = append(hashGroups[p.Hash], p)
		}

		for hash, g := range hashGroups {
			finalGroups[hash] = g
		}
	}

	return finalGroups
}

func chooseBest(group []*models.Photo) *models.Photo {
	sort.Slice(group, func(i, j int) bool {
		if group[i].DateAccuracy < group[j].DateAccuracy {
			return group[i].DateAccuracy < group[j].DateAccuracy
		}
		return len(group[i].SrcPath) < len(group[j].SrcPath)
	})

	return group[0]
}

func MergeIdentical(photos []*models.Photo, progress func(done, total int)) []*models.Photo {
	grouped := GroupIdentical(photos)
	var result []*models.Photo
	total := len(grouped)
	processed := 0

	for _, group := range grouped {
		if len(group) == 1 {
			result = append(result, group[0])
			processed++
			if progress != nil {
				progress(processed, total)
			}
			continue
		}

		best := chooseBest(group)

		best.Albums = make(map[string]bool)
		for _, p := range group {
			for album := range p.Albums {
				best.Albums[album] = true
			}
		}

		result = append(result, best)
		processed++
		if progress != nil {
			progress(processed, total)
		}
	}

	return result
}

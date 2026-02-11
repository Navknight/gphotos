package dedup

import (
	"encoding/json"
	"os"
	"path/filepath"
)

type hashCacheEntry struct {
	Size    int64  `json:"size"`
	MtimeNs int64  `json:"mtime_ns"`
	Hash    string `json:"hash"`
}

type hashCache struct {
	Files map[string]hashCacheEntry `json:"files"`
}

func LoadHashCache(path string) (hashCache, error) {
	if path == "" {
		return hashCache{Files: make(map[string]hashCacheEntry)}, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return hashCache{Files: make(map[string]hashCacheEntry)}, nil
		}
		return hashCache{}, err
	}
	var c hashCache
	if err := json.Unmarshal(data, &c); err != nil {
		return hashCache{}, err
	}
	if c.Files == nil {
		c.Files = make(map[string]hashCacheEntry)
	}
	return c, nil
}

func SaveHashCache(path string, c hashCache) error {
	if path == "" {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o644)
}

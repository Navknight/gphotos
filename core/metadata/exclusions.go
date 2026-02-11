package metadata

import (
	"encoding/json"
	"os"
	"path/filepath"
)

func LoadDateExclusions(path string) (map[string]bool, error) {
	if path == "" {
		return map[string]bool{}, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return map[string]bool{}, nil
		}
		return nil, err
	}
	var list []string
	if err := json.Unmarshal(data, &list); err != nil {
		return nil, err
	}
	ex := make(map[string]bool, len(list))
	for _, item := range list {
		if item != "" {
			ex[item] = true
		}
	}
	return ex, nil
}

func SaveDateExclusions(path string, exclude map[string]bool) error {
	if path == "" {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	list := make([]string, 0, len(exclude))
	for item, ok := range exclude {
		if ok {
			list = append(list, item)
		}
	}
	data, err := json.MarshalIndent(list, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o644)
}

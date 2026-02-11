package albums

import (
	"bufio"
	"fmt"
	"gphotos/core/models"
	"os"
	"sort"
	"strconv"
	"strings"
)

func ListDistinctAlbums(photos []*models.Photo) []string {
	seen := make(map[string]struct{})
	for _, p := range photos {
		if p == nil || p.Albums == nil {
			continue
		}
		for name, ok := range p.Albums {
			if ok {
				seen[name] = struct{}{}
			}
		}
	}

	albums := make([]string, 0, len(seen))
	for name := range seen {
		albums = append(albums, name)
	}
	sort.Strings(albums)
	return albums
}

func PromptAlbumSelection(albums []string) ([]string, error) {
	if len(albums) == 0 {
		fmt.Println("No albums found.")
		return nil, nil
	}

	fmt.Println("Albums found:")
	for i, name := range albums {
		fmt.Printf("%d) %s\n", i+1, name)
	}
	fmt.Println("Enter album numbers or names in priority order.")
	fmt.Println("Examples: 1,3,5  OR  Vacation,Family  OR  all  OR  (empty to keep none)")
	fmt.Print("Selection: ")

	reader := bufio.NewReader(os.Stdin)
	line, err := reader.ReadString('\n')
	if err != nil && err.Error() != "EOF" {
		return nil, err
	}

	line = strings.TrimSpace(line)
	if line == "" {
		return nil, nil
	}
	if strings.EqualFold(line, "all") {
		selected := append([]string(nil), albums...)
		fmt.Printf("Selected albums (priority order): %s\n", strings.Join(selected, ", "))
		return selected, nil
	}

	parts := strings.Split(line, ",")
	selected := make([]string, 0, len(parts))
	seen := make(map[string]struct{})

	albumIndex := make(map[string]int, len(albums))
	for i, name := range albums {
		albumIndex[strings.ToLower(name)] = i
	}

	for _, raw := range parts {
		item := strings.TrimSpace(raw)
		if item == "" {
			continue
		}

		if idx, err := strconv.Atoi(item); err == nil {
			if idx < 1 || idx > len(albums) {
				return nil, fmt.Errorf("album index out of range: %d", idx)
			}
			name := albums[idx-1]
			if _, ok := seen[name]; ok {
				continue
			}
			seen[name] = struct{}{}
			selected = append(selected, name)
			continue
		}

		key := strings.ToLower(item)
		idx, ok := albumIndex[key]
		if !ok {
			return nil, fmt.Errorf("unknown album name: %s", item)
		}
		name := albums[idx]
		if _, ok := seen[name]; ok {
			continue
		}
		seen[name] = struct{}{}
		selected = append(selected, name)
	}

	if len(selected) == 0 {
		fmt.Println("No albums selected. All photos will go to the main library.")
		return nil, nil
	}
	fmt.Printf("Selected albums (priority order): %s\n", strings.Join(selected, ", "))
	return selected, nil
}

// AssignFinalAlbums assigns each photo to at most one final album
// based on the provided priority-ordered selection.
func AssignFinalAlbums(photos []*models.Photo, selected []string, progress func(done, total int)) {
	total := len(photos)
	processed := 0
	for _, p := range photos {
		if p == nil {
			continue
		}
		p.FinalAlbum = ""
		if p.Albums == nil || len(selected) == 0 {
			continue
		}
		for _, name := range selected {
			if p.Albums[name] {
				p.FinalAlbum = name
				break
			}
		}
		if p.FinalAlbum == "" {
			fmt.Printf("Album: (library) <- %s\n", p.SrcPath)
		} else {
			fmt.Printf("Album: %s <- %s\n", p.FinalAlbum, p.SrcPath)
		}
		processed++
		if progress != nil {
			progress(processed, total)
		}
	}
}

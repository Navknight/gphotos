package main

import (
	"bufio"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"gphotos/core/albums"
	"gphotos/core/dedup"
	"gphotos/core/metadata"
	"gphotos/core/models"
	"gphotos/core/output"
	"gphotos/core/scanner"
)

func main() {
	dryRun := flag.Bool("dry-run", false, "Print planned operations without copying files")
	verbose := flag.Bool("verbose", true, "Print progress and file details")
	datesOnly := flag.Bool("dates-only", false, "Only analyze dates (skip hashing, dedup, albums, output)")
	workers := flag.Int("workers", 4, "Number of parallel workers for copy")
	exifBatch := flag.Int("exif-batch", 25, "Batch size for exiftool metadata writes")
	onlyExts := flag.String("only-exts", "", "Comma-separated list of extensions to include (e.g. .mp,.mov,.m4v)")
	flag.Parse()

	inRoot := promptPath("Enter path to Takeout root", "./Takeout")
	outRoot := ""
	if !*datesOnly {
		outRoot = promptPath("Enter output folder", "./Output")
	}

	fmt.Println("Scanning...")
	pairs, err := scanner.ScanTakeout(inRoot, *verbose)
	if err != nil {
		fmt.Println("Scan error:", err)
		return
	}
	if len(pairs) == 0 {
		fmt.Println("No media files found.")
		return
	}
	printScanSummary(pairs)
	if strings.TrimSpace(*onlyExts) != "" {
		pairs = filterPairsByExt(pairs, *onlyExts)
		if len(pairs) == 0 {
			fmt.Println("No media files matched the requested extensions.")
			return
		}
		fmt.Printf("Filtered media by extensions, remaining: %d\n", len(pairs))
	}

	if *datesOnly {
		photos := photosFromScan(pairs)
		if err := applyDatesWithReview(photos); err != nil {
			fmt.Println("Date parsing error:", err)
			return
		}
		fmt.Println("Dates-only analysis complete.")
		return
	}

	fmt.Println("Building registry...")
	hashBar := newProgressBar("Hashing")
	cachePath := filepath.Join(inRoot, ".gphotos", "hash_cache.json")
	registry := dedup.BuildRegistry(pairs, cachePath, *verbose, hashBar.Update)
	hashBar.Finish()
	photos := registryToSlice(registry)
	fmt.Printf("Unique files (by hash): %d\n", len(registry))

	if err := applyDatesWithReview(photos); err != nil {
		fmt.Println("Date parsing error:", err)
		return
	}

	fmt.Println("Merging duplicates...")
	mergeBar := newProgressBar("Merging")
	before := len(photos)
	photos = dedup.MergeIdentical(photos, mergeBar.Update)
	mergeBar.Finish()
	fmt.Printf("Duplicates merged: %d -> %d\n", before, len(photos))

	allAlbums := albums.ListDistinctAlbums(photos)
	fmt.Printf("Distinct albums detected: %d\n", len(allAlbums))
	selected, err := albums.PromptAlbumSelection(allAlbums)
	if err != nil {
		fmt.Println("Album selection error:", err)
		return
	}
	assignBar := newProgressBar("Assigning albums")
	albums.AssignFinalAlbums(photos, selected, assignBar.Update)
	assignBar.Finish()
	printAlbumSummary(photos)

	fmt.Println("Organizing output...")
	copyBar := newProgressBar("Copying")
	if err := output.OrganizePhotos(photos, outRoot, *dryRun, *verbose, *workers, *exifBatch, copyBar.Update); err != nil {
		fmt.Println("Output error:", err)
		return
	}
	copyBar.Finish()

	if *dryRun {
		fmt.Println("Dry run complete.")
	} else {
		fmt.Println("Done.")
	}
}

func filterPairsByExt(pairs []scanner.FilePair, onlyExts string) []scanner.FilePair {
	set := make(map[string]bool)
	for _, part := range strings.Split(onlyExts, ",") {
		ext := strings.ToLower(strings.TrimSpace(part))
		if ext == "" {
			continue
		}
		if !strings.HasPrefix(ext, ".") {
			ext = "." + ext
		}
		set[ext] = true
	}
	if len(set) == 0 {
		return pairs
	}
	out := make([]scanner.FilePair, 0, len(pairs))
	for _, p := range pairs {
		ext := strings.ToLower(filepath.Ext(p.MediaPath))
		if set[ext] {
			out = append(out, p)
		}
	}
	return out
}

type dateProposal struct {
	photo    *models.Photo
	jsonTime time.Time
	fileTime time.Time
	exifTime time.Time
	hasJSON  bool
	hasFile  bool
	hasExif  bool
	proposed time.Time
	accuracy int
}

func applyDatesWithReview(photos []*models.Photo) error {
	patternPath := filepath.Join(".gphotos", "date_patterns.json")
	exclusionPath := filepath.Join(".gphotos", "date_exclusions.json")
	custom, err := metadata.LoadCustomPatterns(patternPath)
	if err != nil {
		return err
	}
	exclusions, err := metadata.LoadDateExclusions(exclusionPath)
	if err != nil {
		return err
	}

	dateBar := newProgressBar("Analyzing dates")
	proposals := collectDateProposals(photos, custom, exclusions, dateBar.Update)
	dateBar.Finish()
	for {
		unknown := filterUnknown(proposals)
		if len(unknown) == 0 {
			break
		}
		updated, updatedExclusions, err := promptCustomPatternsLoop(unknown, custom, exclusions, patternPath, exclusionPath)
		if err != nil {
			return err
		}
		if len(updated) == len(custom) && len(updatedExclusions) == len(exclusions) {
			break
		}
		custom = updated
		exclusions = updatedExclusions
		dateBar = newProgressBar("Analyzing dates")
		proposals = collectDateProposals(photos, custom, exclusions, dateBar.Update)
		dateBar.Finish()
	}

	printDateReview(proposals)
	if !promptApplyConfirmation() {
		return fmt.Errorf("date review not confirmed")
	}

	for _, p := range proposals {
		if p.accuracy == metadata.DateAccuracyNone {
			p.photo.Meta.TakenTime = ""
			p.photo.DateAccuracy = metadata.DateAccuracyNone
			continue
		}
		p.photo.Meta.TakenTime = p.proposed.Format(time.RFC3339)
		p.photo.DateAccuracy = p.accuracy
	}

	return nil
}

func collectDateProposals(photos []*models.Photo, custom []metadata.CustomPattern, exclusions map[string]bool, progress func(done, total int)) []dateProposal {
	proposals := make([]dateProposal, 0, len(photos))
	total := len(photos)
	processed := 0
	for _, p := range photos {
		jsonMeta, hasJSONMeta := metadata.ParseJSONMeta(p.JsonPath)
		jsonTime := jsonMeta.PhotoTakenTime
		hasJSON := jsonMeta.HasPhotoTaken
		if !hasJSON && jsonMeta.HasCreation {
			jsonTime = jsonMeta.CreationTime
			hasJSON = true
		}
		fileTime, hasFile := metadata.GuessDateFromFilenameWithCustomAndExclusions(p.SrcPath, custom, exclusions)
		proposed, accuracy, ok, exifTime, hasExif := metadata.ExtractBestDateWithCustomAndExclusions(p.SrcPath, jsonTime, hasJSON, custom, exclusions)
		if hasJSONMeta {
			if jsonMeta.HasCreation {
				p.Meta.CreationTime = jsonMeta.CreationTime.Format(time.RFC3339)
			}
			p.Meta.Description = jsonMeta.Description
			p.Meta.Favorited = jsonMeta.Favorited
			p.Meta.People = append([]string{}, jsonMeta.People...)
			p.Meta.URL = jsonMeta.URL
			p.Meta.AppSource = jsonMeta.AppSource
			p.Meta.Origin = models.GooglePhotosOrigin{
				FromSharedAlbum:          jsonMeta.Origin.FromSharedAlbum,
				WebUpload:                jsonMeta.Origin.WebUpload,
				MobileUpload:             jsonMeta.Origin.MobileUpload,
				MobileUploadDeviceType:   jsonMeta.Origin.MobileUploadDeviceType,
				MobileUploadDeviceFolder: jsonMeta.Origin.MobileUploadDeviceFolder,
				CompositionType:          jsonMeta.Origin.CompositionType,
			}
			if jsonMeta.HasGeo {
				p.Meta.HasGeo = true
				p.Meta.GPSLat = jsonMeta.Geo.Latitude
				p.Meta.GPSLon = jsonMeta.Geo.Longitude
				p.Meta.GPSAlt = jsonMeta.Geo.Altitude
				p.Meta.GPSSpanLat = jsonMeta.Geo.LatitudeSpan
				p.Meta.GPSSpanLon = jsonMeta.Geo.LongitudeSpan
			}
		}
		if !ok {
			accuracy = metadata.DateAccuracyNone
		}
		proposals = append(proposals, dateProposal{
			photo:    p,
			jsonTime: jsonTime,
			fileTime: fileTime,
			exifTime: exifTime,
			hasJSON:  hasJSON,
			hasFile:  hasFile,
			hasExif:  hasExif,
			proposed: proposed,
			accuracy: accuracy,
		})
		processed++
		if progress != nil {
			progress(processed, total)
		}
	}
	return proposals
}

func filterUnknown(proposals []dateProposal) []dateProposal {
	var out []dateProposal
	for _, p := range proposals {
		if !p.hasJSON && !p.hasFile {
			out = append(out, p)
		}
	}
	return out
}

func printDateReview(proposals []dateProposal) {
	var overrides []dateProposal
	var filenameOnly []dateProposal
	var exifOnly []dateProposal
	var unknown []dateProposal

	for _, p := range proposals {
		switch {
		case p.hasJSON && p.hasFile && p.accuracy == metadata.DateAccuracyFilename:
			overrides = append(overrides, p)
		case !p.hasJSON && p.hasFile:
			filenameOnly = append(filenameOnly, p)
		case !p.hasJSON && !p.hasFile && p.hasExif:
			exifOnly = append(exifOnly, p)
		case !p.hasJSON && !p.hasFile:
			unknown = append(unknown, p)
		}
	}

	fmt.Println("Date review:")
	fmt.Printf("Overrides (filename older than JSON): %d\n", len(overrides))
	for i, p := range overrides {
		fmt.Printf("%d. %s\n", i+1, p.photo.SrcPath)
		fmt.Printf("   JSON: %s  Filename: %s\n", p.jsonTime.Format(time.RFC3339), p.fileTime.Format(time.RFC3339))
	}

	fmt.Printf("Filename-only dates: %d\n", len(filenameOnly))
	for i, p := range filenameOnly {
		fmt.Printf("%d. %s\n", i+1, p.photo.SrcPath)
		fmt.Printf("   Filename: %s\n", p.fileTime.Format(time.RFC3339))
	}

	fmt.Printf("EXIF-only dates: %d\n", len(exifOnly))
	for i, p := range exifOnly {
		fmt.Printf("%d. %s\n", i+1, p.photo.SrcPath)
		fmt.Printf("   EXIF: %s\n", p.exifTime.Format(time.RFC3339))
	}

	fmt.Printf("Unknown dates: %d\n", len(unknown))
	for i, p := range unknown {
		fmt.Printf("%d. %s\n", i+1, p.photo.SrcPath)
	}
}

func promptCustomPatternsLoop(unknown []dateProposal, custom []metadata.CustomPattern, exclusions map[string]bool, path string, exclusionPath string) ([]metadata.CustomPattern, map[string]bool, error) {
	fmt.Printf("Unknown date files detected. You can add custom date regex patterns.\n")
	fmt.Printf("Patterns will be saved to %s\n", path)

	unknownPaths := make([]string, 0, len(unknown))
	for _, p := range unknown {
		unknownPaths = append(unknownPaths, p.photo.SrcPath)
	}

	for {
		fmt.Println("Unknown file groups (by name pattern):")
		printUnknownGroups(unknown, 50)
		fmt.Println("Enter a regex that matches only the date portion.")
		fmt.Println("If you include a capture group, group 1 will be parsed as the date.")
		fmt.Println("Example regex: (20|19)\\d{2}[01]\\d[0-3]\\d_\\d{6}")
		fmt.Println("Special layouts: UNIX (seconds), UNIXMS (milliseconds).")

		regex := promptLine("Date regex (blank to stop)")
		if strings.TrimSpace(regex) == "" {
			break
		}
		layout := promptLine("Time layout for regex match (example: 20060102_150405)")
		if strings.TrimSpace(layout) == "" {
			fmt.Println("Layout is required.")
			continue
		}

		re, err := regexp.Compile(regex)
		if err != nil {
			fmt.Println("Invalid regex:", err)
			continue
		}

		matched, parsed, previews := previewCustomPattern(re, layout, unknownPaths)
		fmt.Printf("Pattern matched %d files, parsed %d dates.\n", matched, parsed)
		if len(previews) > 0 {
			fmt.Println("Preview of parsed dates:")
			for i, p := range previews {
				fmt.Printf("  %d. %s -> %s\n", i+1, p.path, p.date)
			}
		}
		if matched == 0 || parsed == 0 {
			if !promptYesNo("Keep this pattern anyway", false) {
				continue
			}
		}

		decision := promptLine("Accept? all / none / exclude 1,2,3")
		decision = strings.TrimSpace(strings.ToLower(decision))
		if decision == "none" {
			continue
		}
		if decision != "all" && decision != "" {
			excluded, err := parseIndexList(decision, len(previews))
			if err != nil {
				fmt.Println("Invalid exclude list:", err)
				continue
			}
			for _, idx := range excluded {
				if idx < 1 || idx > len(previews) {
					continue
				}
				exclusions[previews[idx-1].path] = true
			}
			if err := metadata.SaveDateExclusions(exclusionPath, exclusions); err != nil {
				return nil, nil, err
			}
		}

		custom = append(custom, metadata.CustomPattern{
			Regex:  regex,
			Layout: layout,
		})
		if err := metadata.SaveCustomPatterns(path, custom); err != nil {
			return nil, nil, err
		}
		break
	}

	return custom, exclusions, nil
}

type previewEntry struct {
	path string
	date string
}

func previewCustomPattern(re *regexp.Regexp, layout string, paths []string) (int, int, []previewEntry) {
	matched := 0
	parsed := 0
	previews := make([]previewEntry, 0, len(paths))
	for _, path := range paths {
		base := filepath.Base(path)
		sub := re.FindStringSubmatch(base)
		if len(sub) == 0 {
			continue
		}
		target := sub[0]
		if len(sub) > 1 {
			target = sub[1]
		}
		matched++
		t, ok := metadata.ParseWithLayout(layout, target)
		if !ok {
			continue
		}
		parsed++
		previews = append(previews, previewEntry{
			path: base,
			date: t.Format(time.RFC3339),
		})
	}
	return matched, parsed, previews
}

func promptApplyConfirmation() bool {
	fmt.Println("Review is required before applying date changes.")
	fmt.Println("Type APPLY to continue, or anything else to cancel.")
	line := promptLine("Confirmation")
	return strings.EqualFold(strings.TrimSpace(line), "APPLY")
}

func promptPath(label, defaultPath string) string {
	reader := bufio.NewReader(os.Stdin)
	if defaultPath != "" {
		fmt.Printf("%s (default: %s): ", label, defaultPath)
	} else {
		fmt.Printf("%s: ", label)
	}
	line, _ := reader.ReadString('\n')
	line = strings.TrimSpace(line)
	if line == "" {
		return defaultPath
	}
	return line
}

func promptLine(label string) string {
	reader := bufio.NewReader(os.Stdin)
	fmt.Printf("%s: ", label)
	line, _ := reader.ReadString('\n')
	return strings.TrimSpace(line)
}

func promptYesNo(label string, defaultYes bool) bool {
	reader := bufio.NewReader(os.Stdin)
	if defaultYes {
		fmt.Printf("%s [Y/n]: ", label)
	} else {
		fmt.Printf("%s [y/N]: ", label)
	}
	line, _ := reader.ReadString('\n')
	line = strings.TrimSpace(strings.ToLower(line))
	if line == "" {
		return defaultYes
	}
	return line == "y" || line == "yes"
}

func registryToSlice(registry map[string]*models.Photo) []*models.Photo {
	photos := make([]*models.Photo, 0, len(registry))
	for _, p := range registry {
		photos = append(photos, p)
	}
	return photos
}

func photosFromScan(pairs []scanner.FilePair) []*models.Photo {
	photos := make([]*models.Photo, 0, len(pairs))
	for _, p := range pairs {
		if p.MediaPath == "" {
			continue
		}
		albumsMap := make(map[string]bool)
		if p.Album != "" {
			albumsMap[p.Album] = true
		}
		photos = append(photos, &models.Photo{
			SrcPath:  p.MediaPath,
			JsonPath: p.JsonPath,
			Albums:   albumsMap,
		})
	}
	return photos
}

func printScanSummary(pairs []scanner.FilePair) {
	withAlbum := 0
	withJSON := 0
	for _, p := range pairs {
		if p.Album != "" {
			withAlbum++
		}
		if p.JsonPath != "" {
			if _, err := os.Stat(p.JsonPath); err == nil {
				withJSON++
			}
		}
	}
	fmt.Printf("Scan summary: %d media files, %d with album, %d with JSON\n", len(pairs), withAlbum, withJSON)
}

func printAlbumSummary(photos []*models.Photo) {
	counts := make(map[string]int)
	for _, p := range photos {
		if p == nil {
			continue
		}
		album := strings.TrimSpace(p.FinalAlbum)
		if album == "" {
			album = "(library)"
		}
		counts[album]++
	}
	fmt.Println("Album assignment summary:")
	for album, count := range counts {
		fmt.Printf("  %s: %d\n", album, count)
	}
}

type unknownGroup struct {
	key      string
	paths    []string
	examples []string
}

func printUnknownGroups(unknown []dateProposal, limit int) {
	if len(unknown) == 0 {
		return
	}
	groups := groupUnknownByPattern(unknown)
	shown := 0
	for _, g := range groups {
		if shown >= limit {
			break
		}
		fmt.Printf("  %s (%d files)\n", g.key, len(g.paths))
		for i := 0; i < len(g.examples); i++ {
			fmt.Printf("    %s\n", g.examples[i])
		}
		shown++
	}
	if len(groups) > limit {
		fmt.Printf("  ... %d more groups\n", len(groups)-limit)
	}
}

func groupUnknownByPattern(unknown []dateProposal) []unknownGroup {
	groupMap := make(map[string]*unknownGroup)
	for _, p := range unknown {
		base := filepath.Base(p.photo.SrcPath)
		key := normalizeNamePattern(base)
		g, ok := groupMap[key]
		if !ok {
			g = &unknownGroup{key: key}
			groupMap[key] = g
		}
		g.paths = append(g.paths, p.photo.SrcPath)
		if len(g.examples) < 3 {
			g.examples = append(g.examples, base)
		}
	}
	groups := make([]unknownGroup, 0, len(groupMap))
	for _, g := range groupMap {
		groups = append(groups, *g)
	}
	sortUnknownGroups(groups)
	return groups
}

func normalizeNamePattern(name string) string {
	var b strings.Builder
	b.Grow(len(name))
	lastWasDigit := false
	for _, r := range name {
		if r >= '0' && r <= '9' {
			if !lastWasDigit {
				b.WriteByte('#')
				lastWasDigit = true
			}
			continue
		}
		lastWasDigit = false
		if r == ' ' || r == '-' || r == '_' || r == '.' {
			b.WriteByte('_')
			continue
		}
		if r >= 'A' && r <= 'Z' {
			r = r - 'A' + 'a'
		}
		b.WriteRune(r)
	}
	return b.String()
}

func sortUnknownGroups(groups []unknownGroup) {
	sort.Slice(groups, func(i, j int) bool {
		if len(groups[i].paths) == len(groups[j].paths) {
			return groups[i].key < groups[j].key
		}
		return len(groups[i].paths) > len(groups[j].paths)
	})
}

func parseIndexList(input string, max int) ([]int, error) {
	input = strings.ReplaceAll(input, "exclude", "")
	input = strings.TrimSpace(input)
	if input == "" {
		return nil, nil
	}
	parts := strings.Split(input, ",")
	var out []int
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		n, err := strconv.Atoi(part)
		if err != nil {
			return nil, err
		}
		if n < 1 || n > max {
			continue
		}
		out = append(out, n)
	}
	return out, nil
}

type progressBar struct {
	label       string
	width       int
	lastPercent int
	lastTime    time.Time
}

func newProgressBar(label string) *progressBar {
	return &progressBar{label: label, width: 30}
}

func (p *progressBar) Update(done, total int) {
	if total <= 0 {
		return
	}
	if done > total {
		done = total
	}
	percent := int(float64(done) / float64(total) * 100)
	now := time.Now()
	if done != total {
		if percent == p.lastPercent && now.Sub(p.lastTime) < 750*time.Millisecond {
			return
		}
		if percent < p.lastPercent+1 && now.Sub(p.lastTime) < 750*time.Millisecond {
			return
		}
	}
	p.lastPercent = percent
	p.lastTime = now

	filled := int(float64(percent) / 100 * float64(p.width))
	if filled > p.width {
		filled = p.width
	}
	bar := strings.Repeat("#", filled) + strings.Repeat("-", p.width-filled)
	fmt.Printf("\r%s [%s] %d/%d", p.label, bar, done, total)
}

func (p *progressBar) Finish() {
	fmt.Println()
}

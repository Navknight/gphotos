package metadata

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"gphotos/core/models"
)

var supportedWriteExt = map[string]bool{
	".jpg":  true,
	".jpeg": true,
	".png":  true,
	".heic": true,
	".heif": true,
	".mp4":  true,
	".mov":  true,
	".m4v":  true,
	".mp":   true,
	".gif":  true,
	".webp": true,
	".dng":  true,
	".nef":  true,
	".mv":   true,
	".mp~2": true,
	".mp~3": true,
}

type WriteItem struct {
	Path string
	Meta models.MetaData
}

type BatchWriter struct {
	cmd   *exec.Cmd
	stdin io.WriteCloser
	mu    sync.Mutex
}

func CanWriteMeta() bool {
	return hasExiftool()
}

func HasWritableMeta(meta models.MetaData) bool {
	if meta.TakenTime != "" || meta.CreationTime != "" || meta.HasGeo || meta.Description != "" || meta.Favorited || meta.URL != "" || meta.AppSource != "" {
		return true
	}
	if len(meta.People) > 0 {
		return true
	}
	if label := buildOriginLabel(meta.Origin); label != "" {
		return true
	}
	return false
}

func WriteMetaToFile(path string, meta models.MetaData) error {
	if path == "" {
		return nil
	}
	if !hasExiftool() {
		return fmt.Errorf("exiftool not available")
	}
	itemArgs, ok := buildArgsForMeta(path, meta)
	if !ok {
		return nil
	}
	args := append([]string{"-overwrite_original", "-q", "-q", "-m"}, itemArgs...)
	cmd := exec.Command("exiftool", args...)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("exiftool failed: %v (%s)", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func WriteMetaBatch(items []WriteItem) error {
	if len(items) == 0 {
		return nil
	}
	if !hasExiftool() {
		return fmt.Errorf("exiftool not available")
	}

	args := []string{"-overwrite_original", "-q", "-q", "-m"}
	wrote := 0
	for _, item := range items {
		if item.Path == "" || !HasWritableMeta(item.Meta) {
			continue
		}
		itemArgs, ok := buildArgsForMeta(item.Path, item.Meta)
		if !ok {
			continue
		}
		args = append(args, itemArgs...)
		args = append(args, "-execute")
		wrote++
	}
	if wrote == 0 {
		return nil
	}
	cmd := exec.Command("exiftool", args...)
	if out, err := cmd.CombinedOutput(); err != nil {
		// Fallback: try items individually to salvage the batch.
		failures := 0
		for _, item := range items {
			if item.Path == "" || !HasWritableMeta(item.Meta) {
				continue
			}
			if err := WriteMetaToFile(item.Path, item.Meta); err != nil {
				failures++
			}
		}
		if failures > 0 {
			return fmt.Errorf("exiftool batch failed: %v (%s); %d item(s) failed in fallback", err, strings.TrimSpace(string(out)), failures)
		}
		return nil
	}
	return nil
}

func isVideoExt(ext string) bool {
	switch ext {
	case ".mp4", ".mov", ".m4v", ".mp", ".mv", ".mp~2", ".mp~3":
		return true
	default:
		return false
	}
}

func buildArgsForMeta(path string, meta models.MetaData) ([]string, bool) {
	ext := strings.ToLower(filepath.Ext(path))
	if !supportedWriteExt[ext] {
		return nil, false
	}
	if !matchesExtension(path, ext) {
		return nil, false
	}
	args := []string{}

	if meta.TakenTime != "" {
		if t, err := time.Parse(time.RFC3339, meta.TakenTime); err == nil {
			ts := t.Format("2006:01:02 15:04:05-07:00")
			args = append(args,
				"-DateTimeOriginal="+ts,
				"-CreateDate="+ts,
			)
			if isVideoExt(ext) {
				args = append(args,
					"-MediaCreateDate="+ts,
					"-TrackCreateDate="+ts,
				)
			}
		}
	}
	if meta.CreationTime != "" {
		if t, err := time.Parse(time.RFC3339, meta.CreationTime); err == nil {
			ts := t.Format("2006:01:02 15:04:05-07:00")
			args = append(args, "-XMP:CreateDate="+ts)
		}
	}
	if meta.HasGeo {
		args = append(args,
			fmt.Sprintf("-GPSLatitude=%f", meta.GPSLat),
			fmt.Sprintf("-GPSLongitude=%f", meta.GPSLon),
			fmt.Sprintf("-GPSAltitude=%f", meta.GPSAlt),
		)
	}
	if meta.Description != "" {
		args = append(args,
			"-ImageDescription="+meta.Description,
			"-XMP:Description="+meta.Description,
		)
	}
	if meta.Favorited {
		args = append(args, "-XMP:Rating=5")
	}
	for _, name := range meta.People {
		if strings.TrimSpace(name) == "" {
			continue
		}
		args = append(args,
			"-XMP:PersonInImage+="+name,
			"-XMP:Subject+="+name,
		)
	}
	if meta.URL != "" {
		args = append(args, "-XMP:Source="+meta.URL)
	}
	if meta.AppSource != "" {
		args = append(args, "-XMP:CreatorTool="+meta.AppSource)
	}
	if label := buildOriginLabel(meta.Origin); label != "" {
		args = append(args, "-XMP:Label="+label)
	}
	if len(args) == 0 {
		return nil, false
	}
	args = append(args, path)
	return args, true
}

func matchesExtension(path string, ext string) bool {
	kind, ok := DetectFileKind(path)
	if !ok {
		return true
	}
	switch ext {
	case ".jpg", ".jpeg":
		return kind == "jpeg"
	case ".png":
		return kind == "png"
	case ".heic", ".heif":
		return kind == "heic"
	default:
		return true
	}
}

func sniffFileKind(path string) (string, bool) {
	f, err := os.Open(path)
	if err != nil {
		return "", false
	}
	defer f.Close()

	buf := make([]byte, 12)
	n, err := f.Read(buf)
	if err != nil || n < 12 {
		return "", false
	}
	if buf[0] == 0xFF && buf[1] == 0xD8 && buf[2] == 0xFF {
		return "jpeg", true
	}
	if buf[0] == 0x89 && buf[1] == 0x50 && buf[2] == 0x4E && buf[3] == 0x47 &&
		buf[4] == 0x0D && buf[5] == 0x0A && buf[6] == 0x1A && buf[7] == 0x0A {
		return "png", true
	}
	if string(buf[4:8]) == "ftyp" {
		brand := string(buf[8:12])
		switch brand {
		case "heic", "heix", "heif", "hevc", "heim", "heis":
			return "heic", true
		}
	}
	if string(buf[0:4]) == "RIFF" && string(buf[8:12]) == "WEBP" {
		return "webp", true
	}
	return "", false
}

// DetectFileKind inspects the file header and returns a normalized kind label.
func DetectFileKind(path string) (string, bool) {
	return sniffFileKind(path)
}

// PreferredExtension returns the canonical extension for a detected kind.
func PreferredExtension(kind string) string {
	switch kind {
	case "jpeg":
		return ".jpg"
	case "png":
		return ".png"
	case "heic":
		return ".heic"
	case "webp":
		return ".webp"
	default:
		return ""
	}
}

// StartBatchWriter launches a persistent exiftool process for fast batched writes.
func StartBatchWriter() (*BatchWriter, error) {
	if !hasExiftool() {
		return nil, fmt.Errorf("exiftool not available")
	}
	cmd := exec.Command("exiftool", "-stay_open", "True", "-common_args", "-overwrite_original", "-q", "-q", "-m", "-@", "-")
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	// Drain output to avoid blocking.
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	go io.Copy(io.Discard, stdout)
	go io.Copy(io.Discard, stderr)
	return &BatchWriter{cmd: cmd, stdin: stdin}, nil
}

// Write sends a batch of metadata updates to the persistent exiftool process.
func (w *BatchWriter) Write(items []WriteItem) error {
	if w == nil || w.stdin == nil {
		return nil
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	for _, item := range items {
		if item.Path == "" || !HasWritableMeta(item.Meta) {
			continue
		}
		args, ok := buildArgsForMeta(item.Path, item.Meta)
		if !ok {
			continue
		}
		for _, a := range args {
			if _, err := fmt.Fprintln(w.stdin, a); err != nil {
				return err
			}
		}
		if _, err := fmt.Fprintln(w.stdin, "-execute"); err != nil {
			return err
		}
	}
	return nil
}

// Close shuts down the persistent exiftool process.
func (w *BatchWriter) Close() error {
	if w == nil || w.stdin == nil {
		return nil
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	_, _ = fmt.Fprintln(w.stdin, "-stay_open")
	_, _ = fmt.Fprintln(w.stdin, "False")
	_ = w.stdin.Close()
	return w.cmd.Wait()
}

func buildOriginLabel(origin models.GooglePhotosOrigin) string {
	var parts []string
	if origin.FromSharedAlbum {
		parts = append(parts, "fromSharedAlbum")
	}
	if origin.WebUpload {
		parts = append(parts, "webUpload")
	}
	if origin.MobileUpload {
		parts = append(parts, "mobileUpload")
	}
	if origin.CompositionType != "" {
		parts = append(parts, "composition="+origin.CompositionType)
	}
	if origin.MobileUploadDeviceType != "" {
		parts = append(parts, "deviceType="+origin.MobileUploadDeviceType)
	}
	if origin.MobileUploadDeviceFolder != "" {
		parts = append(parts, "deviceFolder="+origin.MobileUploadDeviceFolder)
	}
	if len(parts) == 0 {
		return ""
	}
	return "gphotos:" + strings.Join(parts, ",")
}

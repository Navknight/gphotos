package metadata

import (
	"encoding/json"
	"os"
	"strings"
	"time"
)

type JSONMeta struct {
	PhotoTakenTime time.Time
	HasPhotoTaken  bool
	CreationTime   time.Time
	HasCreation    bool
	Description    string
	Favorited      bool
	People         []string
	URL            string
	AppSource      string
	Origin         JSONOrigin
	Geo            JSONGeo
	HasGeo         bool
}

type JSONOrigin struct {
	FromSharedAlbum          bool
	WebUpload                bool
	MobileUpload             bool
	MobileUploadDeviceType   string
	MobileUploadDeviceFolder string
	CompositionType          string
}

type JSONGeo struct {
	Latitude      float64
	Longitude     float64
	Altitude      float64
	LatitudeSpan  float64
	LongitudeSpan float64
}

type jsonTime struct {
	Timestamp any `json:"timestamp"`
}

type jsonGeo struct {
	Latitude      float64 `json:"latitude"`
	Longitude     float64 `json:"longitude"`
	Altitude      float64 `json:"altitude"`
	LatitudeSpan  float64 `json:"latitudeSpan"`
	LongitudeSpan float64 `json:"longitudeSpan"`
}

type jsonPerson struct {
	Name string `json:"name"`
}

type jsonAppSource struct {
	AndroidPackageName string `json:"androidPackageName"`
}

type jsonDeviceFolder struct {
	LocalFolderName string `json:"localFolderName"`
}

type jsonMobileUpload struct {
	DeviceFolder jsonDeviceFolder `json:"deviceFolder"`
	DeviceType   string           `json:"deviceType"`
}

type jsonComposition struct {
	Type string `json:"type"`
}

type jsonOrigin struct {
	Composition     jsonComposition  `json:"composition"`
	FromSharedAlbum map[string]any   `json:"fromSharedAlbum"`
	MobileUpload    jsonMobileUpload `json:"mobileUpload"`
	WebUpload       map[string]any   `json:"webUpload"`
}

type jsonMeta struct {
	Description        string        `json:"description"`
	Favorited          bool          `json:"favorited"`
	PhotoTakenTime     jsonTime      `json:"photoTakenTime"`
	CreationTime       jsonTime      `json:"creationTime"`
	GeoData            jsonGeo       `json:"geoData"`
	People             []jsonPerson  `json:"people"`
	URL                string        `json:"url"`
	AppSource          jsonAppSource `json:"appSource"`
	GooglePhotosOrigin jsonOrigin    `json:"googlePhotosOrigin"`
}

func ParseJSONMeta(jsonPath string) (JSONMeta, bool) {
	if jsonPath == "" {
		return JSONMeta{}, false
	}
	data, err := os.ReadFile(jsonPath)
	if err != nil {
		return JSONMeta{}, false
	}

	var raw jsonMeta
	if err := json.Unmarshal(data, &raw); err != nil {
		return JSONMeta{}, false
	}

	out := JSONMeta{
		Description: strings.TrimSpace(raw.Description),
		Favorited:   raw.Favorited,
		URL:         strings.TrimSpace(raw.URL),
		AppSource:   strings.TrimSpace(raw.AppSource.AndroidPackageName),
	}

	if ts, ok := parseTimestamp(raw.PhotoTakenTime.Timestamp); ok {
		out.PhotoTakenTime = time.Unix(ts, 0)
		out.HasPhotoTaken = true
	}
	if ts, ok := parseTimestamp(raw.CreationTime.Timestamp); ok {
		out.CreationTime = time.Unix(ts, 0)
		out.HasCreation = true
	}

	for _, p := range raw.People {
		name := strings.TrimSpace(p.Name)
		if name == "" {
			continue
		}
		out.People = append(out.People, name)
	}

	if raw.GeoData.Latitude != 0 || raw.GeoData.Longitude != 0 || raw.GeoData.Altitude != 0 {
		out.HasGeo = true
		out.Geo = JSONGeo{
			Latitude:      raw.GeoData.Latitude,
			Longitude:     raw.GeoData.Longitude,
			Altitude:      raw.GeoData.Altitude,
			LatitudeSpan:  raw.GeoData.LatitudeSpan,
			LongitudeSpan: raw.GeoData.LongitudeSpan,
		}
	}

	if raw.GooglePhotosOrigin.FromSharedAlbum != nil {
		out.Origin.FromSharedAlbum = true
	}
	if raw.GooglePhotosOrigin.WebUpload != nil {
		out.Origin.WebUpload = true
	}
	if raw.GooglePhotosOrigin.MobileUpload.DeviceType != "" || raw.GooglePhotosOrigin.MobileUpload.DeviceFolder.LocalFolderName != "" {
		out.Origin.MobileUpload = true
		out.Origin.MobileUploadDeviceType = raw.GooglePhotosOrigin.MobileUpload.DeviceType
		out.Origin.MobileUploadDeviceFolder = raw.GooglePhotosOrigin.MobileUpload.DeviceFolder.LocalFolderName
	}
	if raw.GooglePhotosOrigin.Composition.Type != "" {
		out.Origin.CompositionType = raw.GooglePhotosOrigin.Composition.Type
	}

	return out, true
}

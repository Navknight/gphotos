package models

type MetaData struct {
	TakenTime    string
	CreationTime string
	GPSLat       float64
	GPSLon       float64
	GPSAlt       float64
	GPSSpanLat   float64
	GPSSpanLon   float64
	HasGeo       bool
	Description  string
	Favorited    bool
	People       []string
	URL          string
	AppSource    string
	Origin       GooglePhotosOrigin
}

type GooglePhotosOrigin struct {
	FromSharedAlbum          bool
	WebUpload                bool
	MobileUpload             bool
	MobileUploadDeviceType   string
	MobileUploadDeviceFolder string
	CompositionType          string
}

type Photo struct {
	Hash         string
	HashError    bool
	SrcPath      string
	JsonPath     string
	Meta         MetaData
	Albums       map[string]bool
	FinalAlbum   string
	DateAccuracy int
	Size         int64
}

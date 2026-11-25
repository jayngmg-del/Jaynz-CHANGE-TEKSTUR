# Status Pengujian Paket JAYN GMG

Pengujian dilakukan pada file UnityFS sampel yang diberikan dalam percakapan, bukan pada semua kemungkinan bundle Free Fire.

## Backend API — alur penuh lulus

- GET `/api/health`: HTTP 200; UnityPy terdeteksi dan ASTC Encoder aktif.
- POST `/api/scan`: HTTP 200; menemukan 170 objek dan 51 Texture2D.
- GET texture preview: HTTP 200; PNG berhasil dikembalikan.
- POST texture replace: HTTP 200; payload ASTC hasil encoding berhasil dipatch.
- GET ZIP download: HTTP 200; ZIP berhasil dibuka dan berisi satu file Unity hasil.

## Strict verification pada sampel

- UnityFS: v6
- Unity: 2018.4.11f1
- Kompresi blok: LZMA
- Ukuran sumber: 1.800.621 byte
- Ukuran hasil pada uji API: 1.800.621 byte
- Selisih: 0 byte
- Padding tambahan: tidak ada
- Texture2D sebelum/sesudah: 51 / 51
- Objek sebelum/sesudah: 170 / 170
- Hanya payload tekstur target yang berubah
- File hasil dibuka ulang dengan UnityPy

## Batas pengujian

- Ini adalah hasil uji backend/API pada satu sampel nyata.
- Tidak berarti seluruh variasi UnityFS, kompresi, atau texture format didukung.
- Browser Android dan PC belum diuji pada hosting publik.
- Kompatibilitas langsung di dalam Free Fire belum diuji.

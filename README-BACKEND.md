# JAYN GMG — Backend (mesin pemrosesan)

Folder ini berisi bagian yang tidak dapat dimasukkan ke HTML murni: Python/FastAPI, UnityPy, ASTC Encoder native, patcher UnityFS, dan pemeriksaan file.

## Menjalankan dengan Docker

Di Linux x86-64 yang memiliki Docker Engine dan Docker Compose, salin `.env.example` menjadi `.env`, atur `CORS_ORIGINS` ke domain frontend, lalu jalankan:

```bash
cp .env.example .env
# edit CORS_ORIGINS di .env
docker compose up --build -d
```

API tersedia di `http://HOST:8000`; pemeriksaan: `/api/health`; dokumentasi API: `/docs`.

## Menghubungkan frontend

Buka `index.html` dari paket utama. Tekan ikon pengaturan koneksi, isi URL backend tanpa `/api`, lalu tes dan simpan.

Untuk deployment publik, buat `.env` dari `.env.example` dan atur `CORS_ORIGINS` ke domain frontend yang digunakan, misalnya:

```env
CORS_ORIGINS=https://website-anda.example
```

Jangan biarkan wildcard untuk layanan publik yang digunakan banyak orang. Tambahkan HTTPS, autentikasi atau rate limiting di reverse proxy, batas request di proxy, dan penyimpanan job yang sesuai.

## Dependensi

- Python 3.13
- UnityPy 1.25.4
- FastAPI + Uvicorn
- Pillow, NumPy, LZ4
- ASTC Encoder 5.7.1 Linux x86-64 (`tools/astcenc`)

## Dukungan penggantian saat ini

Penggantian hanya diaktifkan untuk UnityFS v6, block table di awal file, LZMA block table, external `.resS`, ASTC LDR RGB/RGBA, dan payload encoded dengan ukuran yang sama persis. File atau tekstur di luar jalur tersebut akan ditolak untuk replace.

Proses menggunakan salinan job, bukan menimpa file sumber. ZIP hanya dibuat setelah pemeriksaan byte count, struktur, inventaris Texture2D, payload target, dan pembacaan ulang lulus.

Pengujian parser tidak sama dengan uji langsung di dalam Free Fire.

## Menjalankan tes dasar

```bash
python -m unittest discover -s tests
```

Untuk uji bundle sampel, set `JAYN_SAMPLE_BUNDLE` ke file UnityFS v6 sampel yang sah, lalu jalankan `python tests/smoke_sample.py`.

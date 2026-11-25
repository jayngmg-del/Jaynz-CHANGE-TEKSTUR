JAYN GMG — PAKET HTML + BACKEND

ISI PAKET
- index.html: seluruh tampilan, CSS, JavaScript, editor ringan, dan logo JG dalam satu file.
- backend/: mesin pemrosesan yang tidak bisa dijalankan HTML murni (Python/FastAPI, UnityPy, ASTC Encoder, patcher UnityFS, pemeriksaan ukuran, dan pembuat ZIP).

CARA MENJALANKAN BACKEND (SERVER LINUX x86-64)
1. Unggah folder backend ke server yang mendukung Docker dan Docker Compose.
2. Masuk ke folder backend.
3. Salin .env.example menjadi .env, lalu ubah CORS_ORIGINS ke domain tempat index.html di-host. Contoh: https://web-anda.example
4. Jalankan: docker compose up --build -d
5. Tes https://ALAMAT-SERVER/api/health. Pastikan ok=true dan astcenc=true.

CARA MENGHUBUNGKAN HTML
1. Buka index.html di browser, atau unggah ke hosting HTML statis.
2. Tekan ikon roda gigi / KONEKSI MESIN pada website.
3. Masukkan alamat backend, misalnya https://api-domain-anda.example (tanpa /api di belakang).
4. Tekan TES KONEKSI, lalu SIMPAN KONEKSI.
5. Setelah status SERVER SIAP, pilih file Unity.

PEMBAGIAN YANG PERLU DIPAHAMI
- Semua bagian website yang berjalan di browser sudah berada dalam satu index.html.
- Pemrosesan UnityPy, ASTC Encoder native, pembacaan/penulisan bundle, dan ZIP harus tetap berada di backend. HTML murni tidak bisa menjalankan binary Linux atau paket Python server.
- HTML dapat dibuka lokal atau di-host terpisah; backend harus tetap online dan URL-nya diatur melalui ikon roda gigi.

BATASAN
- Backend membutuhkan server Linux x86-64 dengan Docker, atau Python 3.13.
- Jalur replace yang aktif: UnityFS v6, tabel blok di depan, LZMA, resource tekstur eksternal, ASTC LDR RGB/RGBA, dan payload hasil encode yang panjangnya sama dengan payload asli.
- Format lain dapat terdeteksi bila parser mendukung, tetapi tidak otomatis dapat diganti.
- File asli tidak ditimpa. ZIP hanya dibuat jika validasi struktur dan ukuran identik lulus.
- Uji parser dan backend bukan pengganti uji langsung di dalam Free Fire. Kompatibilitas in-game belum diuji.

Dokumentasi backend: backend/README-BACKEND.md
Hasil pengujian API: TEST-STATUS.md

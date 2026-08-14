# Task: Pembuatan Core Pathfinding Engine di Rust menggunakan PyO3 & Maturin

## Konteks & Tujuan
Kita ingin mengimplementasikan inner-loop algoritma pathfinding (Bidirectional Dijkstra / Contraction Hierarchies / A*) menggunakan bahasa **Rust** yang akan di-bind langsung ke Python via **PyO3** dan **Maturin**. 

Pendekatan ini menggunakan arsitektur **In-Process Native Extension** (Pendekatan A) sehingga Rust di-compile menjadi binary module (`.so` / `.pyd`) dan dipanggil langsung di Python tanpa overhead network/HTTP.

## Persyaratan Teknis & Kecepatan

1. **Struktur Data Graph di Rust**
   - Gunakan `Vec<Vec<Edge>>` (Adjacency List) dengan *contiguous memory allocation* di Rust untuk memaksimalkan penggunaan CPU Cache.
   - Definisi `Edge`: `struct Edge { node: usize, weight: u32 }`.
   - Gunakan `std::collections::BinaryHeap` dari Rust standard library sebagai Priority Queue (Min-Heap).

2. **Algoritma Pathfinding**
   - Implementasikan fungsi `run_bidirectional_dijkstra` yang menerima `graph_forward`, `graph_backward`, `start_node`, dan `goal_node`.
   - Traversal harus berjalan dari titik asal (forward) dan titik tujuan (backward) secara bersamaan hingga bertemu di *meeting node*.
   - Lepas Global Interpreter Lock (GIL) Python saat komputasi berat berjalan menggunakan `py.allow_threads(...)` agar multi-threading di FastAPI berjalan lancar.

3. **Bridge PyO3 ke Python**
   - Buat fungsi yang di-expose ke Python:
     `#[pyfunction]`
     `fn calculate_route(start: usize, goal: usize) -> PyResult<(Vec<usize>, u32)>`
   - Bungkus modul Rust dengan macro `#[pymodule]`.

4. **Konfigurasi Build (Maturin)**
   - Buat file `Cargo.toml` dengan dependensi `pyo3 = { version = "0.20", features = ["extension-module"] }`.
   - Konfigurasi `crate-type = ["cdylib"]`.

## Instruksi Pembuatan Kode

1. Buatkan file `Cargo.toml` di root project.
2. Buatkan file `src/lib.rs` berisi seluruh struktur data `BinaryHeap`, algoritma `Bidirectional Dijkstra`, lepas GIL, dan binding PyO3.
3. Berikan instruksi cara mengomputasi/build library biner tersebut menggunakan perintah `maturin develop --release`.
4. Berikan contoh cara mengimpor dan memanggil modul Rust tersebut di file Python `app/services/pathfinding/core_engine.py`.
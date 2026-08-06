use std::cmp::{Ordering, Reverse};
use std::collections::{BinaryHeap, HashMap};
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyModule};

#[derive(Clone, Copy)]
struct Edge {
    node: usize,
    weight: f64,
}

struct GraphData {
    forward: Vec<Vec<Edge>>,
    backward: Vec<Vec<Edge>>,
    ids: Vec<i64>,
    index: HashMap<i64, usize>,
}

#[derive(Clone, Copy, PartialEq)]
struct TotalF64(f64);

impl Eq for TotalF64 {}

impl PartialOrd for TotalF64 {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for TotalF64 {
    fn cmp(&self, other: &Self) -> Ordering {
        self.0.total_cmp(&other.0)
    }
}

fn edge_id(u: i64, v: i64) -> u64 {
    let a: u64 = if u >= 0 {
        u as u64
    } else {
        (2 * (-u) - 1) as u64
    };
    let b: u64 = if v >= 0 {
        v as u64
    } else {
        (2 * (-v) - 1) as u64
    };
    (a + b) * (a + b + 1) / 2 + b
}

fn build_index(
    graph: &Bound<'_, PyDict>,
) -> PyResult<(HashMap<i64, usize>, Vec<i64>)> {
    let mut index: HashMap<i64, usize> = HashMap::new();
    let mut ids: Vec<i64> = Vec::new();
    for (k, _) in graph.iter() {
        let id: i64 = k.extract()?;
        if !index.contains_key(&id) {
            index.insert(id, ids.len());
            ids.push(id);
        }
    }
    Ok((index, ids))
}

fn adjacency_from_dict(
    graph: &Bound<'_, PyDict>,
    index: &HashMap<i64, usize>,
    n: usize,
) -> PyResult<Vec<Vec<Edge>>> {
    let mut adj: Vec<Vec<Edge>> = vec![Vec::new(); n];
    for (k, v) in graph.iter() {
        let u: i64 = k.extract()?;
        let ui = *index.get(&u).unwrap();
        let vdict = v.cast::<PyDict>()?;
        for (nk, nw) in vdict.iter() {
            let vv: i64 = nk.extract()?;
            let w: f64 = nw.extract()?;
            if let Some(&vi) = index.get(&vv) {
                adj[ui].push(Edge {
                    node: vi,
                    weight: w,
                });
            }
        }
    }
    Ok(adj)
}

fn reverse_adjacency(adj: &[Vec<Edge>]) -> Vec<Vec<Edge>> {
    let mut rev: Vec<Vec<Edge>> = vec![Vec::new(); adj.len()];
    for (u, edges) in adj.iter().enumerate() {
        for e in edges {
            rev[e.node].push(Edge {
                node: u,
                weight: e.weight,
            });
        }
    }
    rev
}

fn bidirectional_dijkstra(
    forward: &[Vec<Edge>],
    backward: &[Vec<Edge>],
    ids: &[i64],
    start: i64,
    goal: i64,
    penalties: Option<&HashMap<u64, f64>>,
) -> Option<(Vec<i64>, f64)> {
    let s = ids.iter().position(|&x| x == start)?;
    let g = ids.iter().position(|&x| x == goal)?;
    if s == g {
        return Some((vec![start], 0.0));
    }
    let n = forward.len();
    let inf = f64::INFINITY;
    let mut dist_f = vec![inf; n];
    let mut dist_b = vec![inf; n];
    let mut parent_f: Vec<Option<usize>> = vec![None; n];
    let mut parent_b: Vec<Option<usize>> = vec![None; n];
    let mut pq_f: BinaryHeap<Reverse<(TotalF64, usize)>> = BinaryHeap::new();
    let mut pq_b: BinaryHeap<Reverse<(TotalF64, usize)>> = BinaryHeap::new();
    dist_f[s] = 0.0;
    dist_b[g] = 0.0;
    pq_f.push(Reverse((TotalF64(0.0), s)));
    pq_b.push(Reverse((TotalF64(0.0), g)));

    let mut mu = inf;
    let mut meet: Option<usize> = None;

    loop {
        let f_top = pq_f.peek().map(|r| r.0.0.0).unwrap_or(inf);
        let b_top = pq_b.peek().map(|r| r.0.0.0).unwrap_or(inf);
        if f_top >= mu && b_top >= mu {
            break;
        }
        if f_top <= b_top {
            let Reverse((TotalF64(d), u)) = pq_f.pop().unwrap();
            if d > dist_f[u] {
                continue;
            }
            if dist_b[u] < inf {
                let cand = d + dist_b[u];
                if cand < mu {
                    mu = cand;
                    meet = Some(u);
                }
            }
            let id_u = ids[u];
            for e in &forward[u] {
                let pid = edge_id(id_u, ids[e.node]);
                let mult = penalties
                    .and_then(|p| p.get(&pid).copied())
                    .unwrap_or(1.0);
                let nd = d + e.weight * mult;
                if nd < dist_f[e.node] {
                    dist_f[e.node] = nd;
                    parent_f[e.node] = Some(u);
                    pq_f.push(Reverse((TotalF64(nd), e.node)));
                }
            }
        } else {
            let Reverse((TotalF64(d), u)) = pq_b.pop().unwrap();
            if d > dist_b[u] {
                continue;
            }
            if dist_f[u] < inf {
                let cand = d + dist_f[u];
                if cand < mu {
                    mu = cand;
                    meet = Some(u);
                }
            }
            let id_u = ids[u];
            for e in &backward[u] {
                let pid = edge_id(ids[e.node], id_u);
                let mult = penalties
                    .and_then(|p| p.get(&pid).copied())
                    .unwrap_or(1.0);
                let nd = d + e.weight * mult;
                if nd < dist_b[e.node] {
                    dist_b[e.node] = nd;
                    parent_b[e.node] = Some(u);
                    pq_b.push(Reverse((TotalF64(nd), e.node)));
                }
            }
        }
    }

    let m = meet?;
    let mut path: Vec<i64> = Vec::new();
    let mut cur = m;
    loop {
        path.push(ids[cur]);
        match parent_f[cur] {
            Some(p) => cur = p,
            None => break,
        }
    }
    path.reverse();
    cur = m;
    loop {
        match parent_b[cur] {
            Some(p) => {
                cur = p;
                path.push(ids[cur]);
            }
            None => break,
        }
    }
    Some((path, mu))
}

#[pyclass]
struct RustGraph {
    data: Arc<GraphData>,
}

#[pymethods]
impl RustGraph {
    #[new]
    fn new(graph: &Bound<'_, PyDict>, directed: bool) -> PyResult<Self> {
        let (index, ids) = build_index(graph)?;
        let n = ids.len();
        let forward = adjacency_from_dict(graph, &index, n)?;
        let backward = if directed {
            reverse_adjacency(&forward)
        } else {
            forward.clone()
        };
        Ok(Self {
            data: Arc::new(GraphData {
                forward,
                backward,
                ids,
                index,
            }),
        })
    }

    fn route(
        &self,
        py: Python<'_>,
        start: i64,
        goal: i64,
        penalties: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<(Vec<i64>, f64)> {
        let pen: Option<HashMap<u64, f64>> = match penalties {
            Some(d) => Some(d.extract()?),
            None => None,
        };
        let data = self.data.clone();
        let r = py.detach(move || {
            bidirectional_dijkstra(
                &data.forward,
                &data.backward,
                &data.ids,
                start,
                goal,
                pen.as_ref(),
            )
        });
        Ok(r.unwrap_or_else(|| (vec![], f64::INFINITY)))
    }

    fn has_node(&self, node: i64) -> bool {
        self.data.index.contains_key(&node)
    }

    fn node_count(&self) -> usize {
        self.data.ids.len()
    }
}

#[pyfunction]
#[pyo3(signature = (graph_forward, graph_backward, start, goal, penalties=None))]
fn run_bidirectional_dijkstra(
    py: Python<'_>,
    graph_forward: &Bound<'_, PyDict>,
    graph_backward: &Bound<'_, PyDict>,
    start: i64,
    goal: i64,
    penalties: Option<&Bound<'_, PyDict>>,
) -> PyResult<(Vec<i64>, f64)> {
    let (index, ids) = build_index(graph_forward)?;
    let n = ids.len();
    let forward = adjacency_from_dict(graph_forward, &index, n)?;
    let backward = adjacency_from_dict(graph_backward, &index, n)?;
    let pen: Option<HashMap<u64, f64>> = match penalties {
        Some(d) => Some(d.extract()?),
        None => None,
    };
    let r = py.detach(move || {
        bidirectional_dijkstra(&forward, &backward, &ids, start, goal, pen.as_ref())
    });
    Ok(r.unwrap_or_else(|| (vec![], f64::INFINITY)))
}

#[pyfunction]
fn edge_id_py(u: i64, v: i64) -> u64 {
    edge_id(u, v)
}

#[pymodule]
fn _rust_engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<RustGraph>()?;
    m.add_function(wrap_pyfunction!(run_bidirectional_dijkstra, m)?)?;
    m.add_function(wrap_pyfunction!(edge_id_py, m)?)?;
    Ok(())
}

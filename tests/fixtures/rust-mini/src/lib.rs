pub fn is_adult(age: u32) -> bool {
    age >= 18
}

pub fn clamp(x: i32, lo: i32, hi: i32) -> i32 {
    if x < lo {
        return lo;
    }
    if x > hi {
        return hi;
    }
    x
}

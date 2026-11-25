use std::fmt;
use std::str::FromStr;

use serde::Deserialize;

/// 发布版本号，格式为 `yyyy-mm-dd-n`，按日期和当天序号排序。
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Deserialize)]
#[serde(try_from = "String")]
pub struct Version {
    year: u16,
    month: u8,
    day: u8,
    sequence: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ParseVersionError;

impl Version {
    pub fn new(year: u16, month: u8, day: u8, sequence: u32) -> Result<Self, ParseVersionError> {
        if !(1000..=9999).contains(&year)
            || !(1..=12).contains(&month)
            || day == 0
            || day > days_in_month(year, month)
            || sequence == 0
        {
            return Err(ParseVersionError);
        }
        Ok(Self {
            year,
            month,
            day,
            sequence,
        })
    }
}

impl FromStr for Version {
    type Err = ParseVersionError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        let (date, sequence) = value.rsplit_once('-').ok_or(ParseVersionError)?;
        let mut parts = date.split('-');
        let (Some(year), Some(month), Some(day), None) =
            (parts.next(), parts.next(), parts.next(), parts.next())
        else {
            return Err(ParseVersionError);
        };
        if year.len() != 4
            || month.len() != 2
            || day.len() != 2
            || sequence.is_empty()
            || !sequence.bytes().all(|byte| byte.is_ascii_digit())
            || sequence.starts_with('0')
        {
            return Err(ParseVersionError);
        }
        Self::new(
            year.parse().map_err(|_| ParseVersionError)?,
            month.parse().map_err(|_| ParseVersionError)?,
            day.parse().map_err(|_| ParseVersionError)?,
            sequence.parse().map_err(|_| ParseVersionError)?,
        )
    }
}

impl TryFrom<String> for Version {
    type Error = ParseVersionError;

    fn try_from(value: String) -> Result<Self, Self::Error> {
        value.parse()
    }
}

impl fmt::Display for Version {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{:04}-{:02}-{:02}-{}",
            self.year, self.month, self.day, self.sequence
        )
    }
}

impl fmt::Display for ParseVersionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("版本号应为 yyyy-mm-dd-n")
    }
}

impl std::error::Error for ParseVersionError {}

fn days_in_month(year: u16, month: u8) -> u8 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400)) => {
            29
        }
        2 => 28,
        _ => 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_and_formats() {
        let version: Version = "2026-11-25-2".parse().unwrap();
        assert_eq!(version.to_string(), "2026-11-25-2");
        assert_eq!(Version::new(2026, 11, 25, 2).unwrap(), version);
    }

    #[test]
    fn orders_by_date_then_sequence() {
        let first: Version = "2026-11-25-1".parse().unwrap();
        let second: Version = "2026-11-25-2".parse().unwrap();
        let next_day: Version = "2026-11-26-1".parse().unwrap();
        assert!(first < second);
        assert!(second < next_day);
    }

    #[test]
    fn accepts_leap_day() {
        assert!("2024-02-29-1".parse::<Version>().is_ok());
        assert!("2025-02-29-1".parse::<Version>().is_err());
    }

    #[test]
    fn rejects_malformed_versions() {
        for value in [
            "",
            "0.0.0",
            "2026-1-1-1",
            "2026-13-01-1",
            "2026-11-31-1",
            "2026-11-25-0",
            "2026-11-25-01",
            "2026-11-25",
            "2026-11-25-1-1",
            "dev-yyyy-mm-dd-n",
        ] {
            assert!(
                value.parse::<Version>().is_err(),
                "{value} should be rejected"
            );
        }
    }
}

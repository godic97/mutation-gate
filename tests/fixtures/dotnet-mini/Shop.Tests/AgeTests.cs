using Shop;

namespace Shop.Tests;

public class AgeTests
{
    [Fact]
    public void AdultFromEighteen()
    {
        Assert.False(Age.IsAdult(17));
        Assert.True(Age.IsAdult(18));
    }
}
